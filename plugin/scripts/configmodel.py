from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

DEFAULT_HOST_ID = "main"
DEFAULT_ROOT = "/home/ubuntu"

DEFAULT_PROJECTS: dict[str, dict[str, Any]] = {
    "agent-runtime": {"host": DEFAULT_HOST_ID, "path": "/home/ubuntu/agent-runtime"},
    "virtualbrain": {"host": DEFAULT_HOST_ID, "path": "/home/ubuntu/virtualbrain/current"},
    "ferro": {
        "host": DEFAULT_HOST_ID,
        "path": "/home/ubuntu/wechat-traffic-agent",
        "units": ["content-agent.service"],
    },
    "trading": {"host": DEFAULT_HOST_ID, "path": "/home/ubuntu/trading"},
}


def posix_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("path is required")
    return str(PurePosixPath(text))


def join_project_path(root: str, path: str) -> str:
    rel = str(path or "").strip().replace("\\", "/")
    if not rel:
        return posix_path(root)
    parsed = PurePosixPath(rel)
    if ".." in parsed.parts:
        raise PermissionError("parent-directory segments are not allowed in project-relative paths")
    if parsed.is_absolute():
        return posix_path(rel)
    return posix_path(str(PurePosixPath(root) / parsed))


def _host_entry(raw: dict[str, Any], host_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"host {host_id!r} must be an object")
    ssh_host = str(raw.get("ssh_host") or "").strip()
    if not ssh_host or ssh_host.startswith("-") or any(ch.isspace() for ch in ssh_host):
        raise RuntimeError(f"host {host_id!r} has an invalid ssh_host")
    roots_raw = raw.get("roots") or [DEFAULT_ROOT]
    if not isinstance(roots_raw, list):
        raise RuntimeError(f"host {host_id!r} roots must be an array")
    roots = [posix_path(x) for x in roots_raw if str(x).strip()]
    if not roots:
        raise RuntimeError(f"host {host_id!r} roots must not be empty")
    units_raw = raw.get("units") or raw.get("systemd_units") or []
    if not isinstance(units_raw, list):
        raise RuntimeError(f"host {host_id!r} units must be an array")
    units = [str(x).strip() for x in units_raw if str(x).strip()]
    return {"id": host_id, "ssh_host": ssh_host, "roots": roots, "units": units}


def _project_entry(name: str, raw: dict[str, Any], hosts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"project {name!r} must be an object")
    host_id = str(raw.get("host") or DEFAULT_HOST_ID).strip() or DEFAULT_HOST_ID
    if host_id not in hosts:
        raise RuntimeError(f"project {name!r} references unknown host {host_id!r}")
    path = posix_path(str(raw.get("path") or ""))
    host_roots = tuple(hosts[host_id]["roots"])
    if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in host_roots):
        raise RuntimeError(f"project {name!r} path {path} is outside host {host_id!r} roots")
    units_raw = raw.get("units") or []
    if not isinstance(units_raw, list):
        raise RuntimeError(f"project {name!r} units must be an array")
    units = [str(x).strip() for x in units_raw if str(x).strip()]
    return {"name": name, "host": host_id, "path": path, "units": units}


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    if "hosts" in data:
        hosts_raw = data.get("hosts") or {}
        if not isinstance(hosts_raw, dict) or not hosts_raw:
            raise RuntimeError("hosts must be a non-empty object")
        hosts = {str(key): _host_entry(value, str(key)) for key, value in hosts_raw.items()}
        default_host = str(data.get("default_host") or DEFAULT_HOST_ID)
        if default_host not in hosts:
            default_host = next(iter(hosts))
        projects_raw = data.get("projects") or {}
        if not isinstance(projects_raw, dict):
            raise RuntimeError("projects must be an object")
        projects = {
            str(name): _project_entry(str(name), value, hosts)
            for name, value in projects_raw.items()
        }
        tunnel_id = str(data.get("tunnel_id") or "").strip() or None
        return {
            "default_host": default_host,
            "hosts": hosts,
            "projects": projects,
            "tunnel_id": tunnel_id,
        }

    ssh_host = str(data.get("ssh_host") or "").strip()
    if not ssh_host:
        raise RuntimeError("remote config is missing ssh_host or hosts")
    legacy_host = _host_entry(
        {
            "ssh_host": ssh_host,
            "roots": data.get("roots") or [DEFAULT_ROOT],
            "units": data.get("systemd_units") or [],
        },
        DEFAULT_HOST_ID,
    )
    return {
        "default_host": DEFAULT_HOST_ID,
        "hosts": {DEFAULT_HOST_ID: legacy_host},
        "projects": {},
        "tunnel_id": str(data.get("tunnel_id") or "").strip() or None,
    }


def seed_projects(cfg: dict[str, Any]) -> dict[str, Any]:
    hosts = cfg["hosts"]
    projects = dict(cfg["projects"])
    for name, spec in DEFAULT_PROJECTS.items():
        if name not in projects:
            projects[name] = _project_entry(name, spec, hosts)
    cfg = dict(cfg)
    cfg["projects"] = projects
    return cfg


def to_storage(cfg: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "default_host": cfg["default_host"],
        "hosts": {
            host_id: {
                "ssh_host": host["ssh_host"],
                "roots": list(host["roots"]),
                "units": list(host["units"]),
            }
            for host_id, host in cfg["hosts"].items()
        },
        "projects": {
            name: {
                "host": project["host"],
                "path": project["path"],
                **({"units": list(project["units"])} if project["units"] else {}),
            }
            for name, project in cfg["projects"].items()
        },
    }
    if cfg.get("tunnel_id"):
        payload["tunnel_id"] = cfg["tunnel_id"]
    return payload


def default_host(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg["hosts"][cfg["default_host"]]


def host_for(cfg: dict[str, Any], project: str | None = None, host_id: str | None = None) -> dict[str, Any]:
    if project:
        return cfg["hosts"][require_project(cfg, project)["host"]]
    if host_id:
        if host_id not in cfg["hosts"]:
            raise RuntimeError(f"unknown host {host_id!r}")
        return cfg["hosts"][host_id]
    return default_host(cfg)


def require_project(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    key = str(name or "").strip()
    if key not in cfg["projects"]:
        known = ", ".join(sorted(cfg["projects"]) or ["(none)"])
        raise RuntimeError(f"unknown project {key!r}; configured projects: {known}")
    return cfg["projects"][key]


def all_units(cfg: dict[str, Any], *, project: str | None = None, host_id: str | None = None) -> set[str]:
    host = host_for(cfg, project=project, host_id=host_id)
    units = set(host["units"])
    for item in cfg["projects"].values():
        if item["host"] == host["id"]:
            units.update(item["units"])
    if project:
        units.update(require_project(cfg, project)["units"])
    return units


def resolve_path(cfg: dict[str, Any], path: str | None, *, project: str | None = None) -> str:
    if project:
        root = require_project(cfg, project)["path"]
        if path:
            return join_project_path(root, path)
        return root
    if not path:
        raise ValueError("path is required unless project is set")
    return posix_path(path)


def resolve_unit(cfg: dict[str, Any], unit: str | None, *, project: str | None = None) -> str:
    allowed = all_units(cfg, project=project)
    if unit:
        name = str(unit).strip()
        if name not in allowed:
            raise PermissionError("unit is not allowlisted")
        return name
    if project:
        units = require_project(cfg, project)["units"]
        if len(units) == 1:
            return units[0]
    raise ValueError("unit is required unless the project has exactly one configured unit")


def list_projects(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name, project in sorted(cfg["projects"].items()):
        host = cfg["hosts"][project["host"]]
        rows.append(
            {
                "name": name,
                "host": project["host"],
                "ssh_host": host["ssh_host"],
                "path": project["path"],
                "units": list(project["units"]),
            }
        )
    return rows

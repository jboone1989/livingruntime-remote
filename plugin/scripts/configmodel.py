from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
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

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def path_style(value: str) -> str:
    text = str(value or "").strip()
    if _WINDOWS_DRIVE.match(text) or text.startswith("\\\\"):
        return "windows"
    return "posix"


def posix_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("path is required")
    return str(PurePosixPath(text))


def windows_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("path is required")
    return str(PureWindowsPath(text))


def normalize_workspace_path(value: str, style: str | None = None) -> str:
    selected = style or path_style(value)
    if selected == "windows":
        return windows_path(value)
    if selected == "posix":
        return posix_path(value)
    raise ValueError("path_style must be windows or posix")


def _path_type(style: str):
    return PureWindowsPath if style == "windows" else PurePosixPath


def _is_absolute(value: str, style: str) -> bool:
    return _path_type(style)(value).is_absolute()


def _inside_root(path: str, root: str, style: str) -> bool:
    path_type = _path_type(style)
    try:
        path_type(path).relative_to(path_type(root))
        return True
    except ValueError:
        return False


def join_project_path(root: str, path: str) -> str:
    style = path_style(root)
    rel = str(path or "").strip()
    if not rel:
        return normalize_workspace_path(root, style)
    parsed = _path_type(style)(rel)
    if ".." in parsed.parts:
        raise PermissionError("parent-directory segments are not allowed in project-relative paths")
    if parsed.is_absolute():
        return normalize_workspace_path(rel, style)
    return normalize_workspace_path(str(_path_type(style)(root) / parsed), style)


def _host_entry(raw: dict[str, Any], host_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"host {host_id!r} must be an object")
    transport = str(raw.get("transport") or "ssh").strip().lower()
    if transport not in {"ssh", "local"}:
        raise RuntimeError(f"host {host_id!r} has unsupported transport {transport!r}")

    ssh_host = str(raw.get("ssh_host") or ("local" if transport == "local" else "")).strip()
    if transport == "ssh":
        if not ssh_host or ssh_host.startswith("-") or any(ch.isspace() for ch in ssh_host):
            raise RuntimeError(f"host {host_id!r} has an invalid ssh_host")
    elif not ssh_host:
        ssh_host = "local"

    roots_raw = raw.get("roots") or [DEFAULT_ROOT]
    if not isinstance(roots_raw, list):
        raise RuntimeError(f"host {host_id!r} roots must be an array")
    explicit_style = str(raw.get("path_style") or "").strip().lower() or None
    if transport == "ssh":
        selected_style = "posix"
    else:
        selected_style = explicit_style or path_style(str(roots_raw[0]))
    if selected_style not in {"windows", "posix"}:
        raise RuntimeError(f"host {host_id!r} has invalid path_style")

    roots = [
        normalize_workspace_path(str(value), selected_style)
        for value in roots_raw
        if str(value).strip()
    ]
    if not roots:
        raise RuntimeError(f"host {host_id!r} roots must not be empty")
    for root in roots:
        if not _is_absolute(root, selected_style):
            raise RuntimeError(f"host {host_id!r} root {root!r} must be absolute")

    units_raw = raw.get("units") or raw.get("systemd_units") or []
    if not isinstance(units_raw, list):
        raise RuntimeError(f"host {host_id!r} units must be an array")
    units = [str(x).strip() for x in units_raw if str(x).strip()]
    if transport == "local" and selected_style == "windows":
        units = []

    return {
        "id": host_id,
        "transport": transport,
        "ssh_host": ssh_host,
        "path_style": selected_style,
        "roots": roots,
        "units": units,
    }


def _project_entry(
    name: str, raw: dict[str, Any], hosts: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"project {name!r} must be an object")
    host_id = str(raw.get("host") or DEFAULT_HOST_ID).strip() or DEFAULT_HOST_ID
    if host_id not in hosts:
        raise RuntimeError(f"project {name!r} references unknown host {host_id!r}")
    host = hosts[host_id]
    style = host["path_style"]
    path = normalize_workspace_path(str(raw.get("path") or ""), style)
    host_roots = tuple(host["roots"])
    if not any(_inside_root(path, root, style) for root in host_roots):
        raise RuntimeError(f"project {name!r} path {path} is outside host {host_id!r} roots")
    units_raw = raw.get("units") or []
    if not isinstance(units_raw, list):
        raise RuntimeError(f"project {name!r} units must be an array")
    units = [str(x).strip() for x in units_raw if str(x).strip()]
    if host["transport"] == "local" and host["path_style"] == "windows":
        units = []
    return {"name": name, "host": host_id, "path": path, "units": units}


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    if "hosts" in data:
        hosts_raw = data.get("hosts") or {}
        if not isinstance(hosts_raw, dict) or not hosts_raw:
            raise RuntimeError("hosts must be a non-empty object")
        hosts = {
            str(key): _host_entry(value, str(key)) for key, value in hosts_raw.items()
        }
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
    if hosts[cfg["default_host"]]["transport"] != "ssh":
        return cfg
    for name, spec in DEFAULT_PROJECTS.items():
        if name not in projects:
            projects[name] = _project_entry(name, spec, hosts)
    cfg = dict(cfg)
    cfg["projects"] = projects
    return cfg


def to_storage(cfg: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "default_host": cfg["default_host"],
        "hosts": {},
        "projects": {
            name: {
                "host": project["host"],
                "path": project["path"],
                **({"units": list(project["units"])} if project["units"] else {}),
            }
            for name, project in cfg["projects"].items()
        },
    }
    for host_id, host in cfg["hosts"].items():
        item: dict[str, Any] = {
            "ssh_host": host["ssh_host"],
            "roots": list(host["roots"]),
            "units": list(host["units"]),
        }
        if host.get("transport") != "ssh":
            item["transport"] = host["transport"]
        if host.get("path_style") != "posix":
            item["path_style"] = host["path_style"]
        payload["hosts"][host_id] = item
    if cfg.get("tunnel_id"):
        payload["tunnel_id"] = cfg["tunnel_id"]
    return payload


def default_host(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg["hosts"][cfg["default_host"]]


def host_for(
    cfg: dict[str, Any], project: str | None = None, host_id: str | None = None
) -> dict[str, Any]:
    if project:
        project_host_id = require_project(cfg, project)["host"]
        if host_id and host_id != project_host_id:
            raise RuntimeError(
                f"project {project!r} is bound to host {project_host_id!r}, not {host_id!r}"
            )
        return cfg["hosts"][project_host_id]
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


def all_units(
    cfg: dict[str, Any], *, project: str | None = None, host_id: str | None = None
) -> set[str]:
    host = host_for(cfg, project=project, host_id=host_id)
    units = set(host["units"])
    for item in cfg["projects"].values():
        if item["host"] == host["id"]:
            units.update(item["units"])
    if project:
        units.update(require_project(cfg, project)["units"])
    return units


def resolve_path(
    cfg: dict[str, Any],
    path: str | None,
    *,
    project: str | None = None,
    host_id: str | None = None,
) -> str:
    host = host_for(cfg, project=project, host_id=host_id)
    if project:
        root = require_project(cfg, project)["path"]
        if path:
            return join_project_path(root, path)
        return root
    if not path:
        raise ValueError("path is required unless project is set")
    return normalize_workspace_path(path, host["path_style"])


def resolve_unit(
    cfg: dict[str, Any],
    unit: str | None,
    *,
    project: str | None = None,
    host_id: str | None = None,
) -> str:
    allowed = all_units(cfg, project=project, host_id=host_id)
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
                "transport": host["transport"],
                "ssh_host": host["ssh_host"],
                "path": project["path"],
                "units": list(project["units"]),
            }
        )
    return rows

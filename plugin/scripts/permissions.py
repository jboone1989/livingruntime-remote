from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

STORE_VERSION = 1

PENDING_TTL_SECONDS = 24 * 60 * 60
MAX_HISTORY = 500
SNAPSHOT_LIMIT = 50


def _maintain(store: dict[str, Any], *, now: float | None = None) -> bool:
    """Expire abandoned requests; compact decided history, never active grants."""
    current = time.time() if now is None else now
    changed = False
    for request in store["requests"]:
        if request.get("status") != "PENDING":
            continue
        if current - float(request.get("created_at") or current) >= PENDING_TTL_SECONDS:
            request["status"] = "EXPIRED"
            request["decided_at"] = current
            changed = True
            continue
        if any(
            grant.get("revoked_at") is None
            and grant.get("argv") == request.get("argv")
            and grant.get("project") == request.get("project")
            and grant.get("cwd") == request.get("cwd")
            and grant.get("host_id") == request.get("host_id")
            and bool(grant.get("detached")) == bool(request.get("detached"))
            for grant in store["grants"]
        ):
            request["status"] = "SUPERSEDED"
            request["decided_at"] = current
            changed = True
    pending = [r for r in store["requests"] if r.get("status") == "PENDING"]
    history = [r for r in store["requests"] if r.get("status") != "PENDING"]
    if len(history) > MAX_HISTORY:
        store["requests"] = pending + history[-MAX_HISTORY:]
        changed = True
    return changed


READ_ONLY_DIAGNOSTICS = frozenset({
    "free", "uptime", "nproc", "uname", "lscpu", "lsblk", "vmstat",
    "iostat", "whoami", "id",
})

# These are intentionally not dynamically grantable through exec. Dedicated
# bounded tools should be used where one exists.
HARD_DENY_EXECUTABLES = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "su",
    "rm", "rmdir", "dd", "mkfs", "mount", "umount",
    "chmod", "chown", "kill", "pkill", "reboot", "shutdown",
    "systemctl", "journalctl",
})

# Sudo is an explicit administrative capability, never a built-in development
# command or a diagnostic-class grant. Keep its non-interactive entry points
# small; the complete argv, host, project and cwd still require an exact grant.
SUDO_TARGETS = frozenset({
    "docker", "apt", "apt-get", "apt-cache", "dpkg", "nginx", "caddy",
    "id", "whoami", "true",
})


def sudo_target(argv: list[str]) -> list[str] | None:
    if not argv or os.path.basename(str(argv[0])) != "sudo":
        return None
    if len(argv) < 3 or argv[1] not in {"-n", "--non-interactive"}:
        raise PermissionError("sudo requires -n or --non-interactive and a command")
    target = argv[2:]
    if target and target[0] == "--":
        target = target[1:]
    if not target or os.path.basename(str(target[0])) not in SUDO_TARGETS:
        raise PermissionError("sudo target is not an approved administrative executable")
    return target


def permission_path() -> Path:
    raw = os.environ.get("LIVINGRUNTIME_REMOTE_PERMISSIONS")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".livingruntime" / "remote-exec-permissions.json"


def classify(argv: list[str], *, detached: bool = False) -> str:
    if not argv:
        raise ValueError("argv is required")
    executable = os.path.basename(str(argv[0]))
    if executable == "sudo":
        try:
            sudo_target(argv)
        except PermissionError:
            return "hard_deny"
        return "explicit_host_approval"
    if executable in HARD_DENY_EXECUTABLES:
        return "hard_deny"
    if detached:
        return "explicit_host_approval"
    if executable in READ_ONLY_DIAGNOSTICS:
        return "read_only_diagnostic"
    return "explicit_host_approval"


def request_digest(
    *,
    host_id: str,
    project: str | None,
    cwd: str,
    argv: list[str],
    detached: bool = False,
) -> str:
    payload = {
        "host_id": host_id,
        "project": project,
        "cwd": cwd,
        "argv": [str(x) for x in argv],
        "detached": bool(detached),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def ensure_request(
    *,
    host_id: str,
    project: str | None,
    cwd: str,
    argv: list[str],
    detached: bool = False,
) -> dict[str, Any]:
    risk = classify(argv, detached=detached)
    if risk == "hard_deny":
        raise PermissionError(
            f"executable is hard-denied from dynamic exec authorization: {os.path.basename(str(argv[0]))}"
        )
    store = _load()
    if _maintain(store):
        _save(store)
    digest = request_digest(
        host_id=host_id, project=project, cwd=cwd, argv=argv, detached=detached
    )
    for item in store["requests"]:
        if item.get("digest") == digest and item.get("status") == "PENDING":
            return dict(item)
    now = time.time()
    request_id = "exec_req_" + hashlib.sha256(
        f"{digest}:{now}".encode("utf-8")
    ).hexdigest()[:20]
    item = {
        "request_id": request_id,
        "created_at": now,
        "status": "PENDING",
        "host_id": host_id,
        "project": project,
        "cwd": cwd,
        "argv": [str(x) for x in argv],
        "detached": bool(detached),
        "executable": os.path.basename(str(argv[0])),
        "risk": risk,
        "digest": digest,
    }
    store["requests"].append(item)
    _save(store)
    return dict(item)


def is_granted(
    *,
    host_id: str,
    project: str | None,
    cwd: str,
    argv: list[str],
    detached: bool = False,
) -> dict[str, Any] | None:
    risk = classify(argv, detached=detached)
    if risk == "hard_deny":
        return None
    store = _load()
    normalized = [str(x) for x in argv]
    for grant in store["grants"]:
        if grant.get("revoked_at") is not None:
            continue
        scope = grant.get("scope")
        if scope == "host" and grant.get("host_id") != host_id:
            continue
        if scope not in {"host", "all_owned_hosts"}:
            continue
        mode = grant.get("grant_mode", "exact")
        if mode == "diagnostic_class":
            if risk != "read_only_diagnostic":
                continue
        elif grant.get("argv") != normalized:
            continue
        elif bool(grant.get("detached", False)) != bool(detached):
            continue
        if grant.get("risk") != "read_only_diagnostic":
            if grant.get("project") != project:
                continue
            if grant.get("cwd") != cwd:
                continue
        return dict(grant)
    return None


def approve(
    request_id: str,
    *,
    scope: str = "host",
    grant_mode: str = "exact",
    operator: str = "human",
) -> dict[str, Any]:
    if scope not in {"host", "all_owned_hosts"}:
        raise ValueError("scope must be host or all_owned_hosts")
    if grant_mode not in {"exact", "diagnostic_class"}:
        raise ValueError("grant_mode must be exact or diagnostic_class")
    store = _load()
    if _maintain(store):
        _save(store)
    request = next((x for x in store["requests"] if x.get("request_id") == request_id), None)
    if request is None:
        raise KeyError(request_id)
    if request.get("status") not in {"PENDING", "APPROVED"}:
        raise RuntimeError("request is no longer pending approval")
    risk = str(request.get("risk"))
    if risk == "hard_deny":
        raise PermissionError("hard-denied commands cannot be approved")
    if (scope == "all_owned_hosts" or grant_mode == "diagnostic_class") and risk != "read_only_diagnostic":
        raise PermissionError(
            "only read_only_diagnostic requests may receive all-host or diagnostic-class grants"
        )

    for grant in store["grants"]:
        if grant.get("request_id") == request_id and grant.get("revoked_at") is None:
            request["status"] = "APPROVED"
            _save(store)
            return dict(grant)

    if request.get("status") != "PENDING":
        raise RuntimeError("request is no longer pending approval")

    now = time.time()
    permission_id = "exec_perm_" + hashlib.sha256(
        f"{request_id}:{scope}:{grant_mode}:{now}".encode("utf-8")
    ).hexdigest()[:20]
    grant = {
        "permission_id": permission_id,
        "request_id": request_id,
        "approved_at": now,
        "approved_by": operator,
        "scope": scope,
        "grant_mode": grant_mode,
        "host_id": request.get("host_id") if scope == "host" else None,
        "project": request.get("project") if grant_mode == "exact" else None,
        "cwd": request.get("cwd") if grant_mode == "exact" else None,
        "argv": list(request.get("argv") or []) if grant_mode == "exact" else None,
        "detached": bool(request.get("detached", False)) if grant_mode == "exact" else False,
        "risk": risk,
        "revoked_at": None,
    }
    request["status"] = "APPROVED"
    request["decided_at"] = now
    store["grants"].append(grant)
    _save(store)
    return dict(grant)


def deny(request_id: str, *, operator: str = "human") -> dict[str, Any]:
    store = _load()
    request = next((x for x in store["requests"] if x.get("request_id") == request_id), None)
    if request is None:
        raise KeyError(request_id)
    request["status"] = "DENIED"
    request["decided_at"] = time.time()
    request["decided_by"] = operator
    _save(store)
    return dict(request)


def revoke(permission_id: str, *, operator: str = "human") -> dict[str, Any]:
    store = _load()
    grant = next((x for x in store["grants"] if x.get("permission_id") == permission_id), None)
    if grant is None:
        raise KeyError(permission_id)
    if grant.get("revoked_at") is None:
        grant["revoked_at"] = time.time()
        grant["revoked_by"] = operator
        _save(store)
    return dict(grant)


def snapshot() -> dict[str, Any]:
    """Return a relay-safe preview while preserving full counts."""
    store = _load()
    if _maintain(store):
        _save(store)
    pending = [dict(x) for x in store["requests"] if x.get("status") == "PENDING"]
    grants = [dict(x) for x in store["grants"] if x.get("revoked_at") is None]
    revoked = [dict(x) for x in store["grants"] if x.get("revoked_at") is not None]
    groups = {"pending": pending, "grants": grants, "revoked": revoked}
    counts = {key: len(value) for key, value in groups.items()}
    previews = {"pending": [], "grants": [], "revoked": []}
    # Leave relay-envelope headroom; a single verbose argv may dwarf 50 records.
    byte_budget = 32_000
    for key, max_items in (("pending", 50), ("grants", 50), ("revoked", 10)):
        for item in reversed(groups[key][-max_items:]):
            encoded = json.dumps(item, ensure_ascii=False)
            size = len(encoded.encode("utf-8"))
            if size > byte_budget:
                continue
            previews[key].append(item)
            byte_budget -= size
        previews[key].reverse()
    return {
        "version": STORE_VERSION,
        "counts": counts,
        **previews,
        "truncated": any(len(previews[key]) != counts[key] for key in groups),
    }


def _load() -> dict[str, Any]:
    path = permission_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": STORE_VERSION, "requests": [], "grants": []}
    if not isinstance(value, dict):
        raise RuntimeError("exec permission store must be a JSON object")
    if int(value.get("version", 0)) != STORE_VERSION:
        raise RuntimeError("unsupported exec permission store version")
    requests = value.get("requests")
    grants = value.get("grants")
    if not isinstance(requests, list) or not isinstance(grants, list):
        raise RuntimeError("invalid exec permission store")
    return {"version": STORE_VERSION, "requests": requests, "grants": grants}


def _save(store: dict[str, Any]) -> None:
    path = permission_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(store, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)

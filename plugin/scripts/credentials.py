from __future__ import annotations

import getpass
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

STORE_VERSION = 1
HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_SECRET_BYTES = 65536


def credential_root() -> Path:
    raw = os.environ.get("LIVINGRUNTIME_REMOTE_CREDENTIALS")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".livingruntime" / "credentials"


def _handle(value: str) -> str:
    text = str(value).strip()
    if not HANDLE_RE.fullmatch(text):
        raise ValueError("credential handle must contain only letters, digits, dot, underscore, or dash")
    return text


def _meta_path(handle: str) -> Path:
    return credential_root() / (_handle(handle) + ".json")


def _secret_path(handle: str) -> Path:
    return credential_root() / (_handle(handle) + ".secret")


def _lease_path() -> Path:
    return credential_root() / "leases.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _normalize_scope(values: list[str] | tuple[str, ...] | None, *, field: str) -> list[str]:
    if values is None:
        return []
    rows: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        if len(item) > 256:
            raise ValueError(f"{field} entry is too long")
        rows.append(item)
    return sorted(set(rows))


def set_local_secret(
    handle: str,
    secret: str,
    *,
    provider: str,
    capabilities: list[str] | tuple[str, ...],
    projects: list[str] | tuple[str, ...] | None = None,
    devices: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    handle = _handle(handle)
    secret = str(secret)
    encoded = secret.encode("utf-8")
    if not encoded:
        raise ValueError("secret must not be empty")
    if len(encoded) > MAX_SECRET_BYTES:
        raise ValueError("secret is too large")
    provider = str(provider).strip()
    if not provider or len(provider) > 128:
        raise ValueError("provider must be a bounded non-empty string")
    allowed_capabilities = _normalize_scope(capabilities, field="capabilities")
    if not allowed_capabilities:
        raise ValueError("at least one capability is required")
    allowed_projects = _normalize_scope(projects, field="projects")
    allowed_devices = _normalize_scope(devices, field="devices")

    root = credential_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass

    previous = _load_meta(handle, missing_ok=True)
    now = time.time()
    meta = {
        "version": STORE_VERSION,
        "handle": handle,
        "provider": provider,
        "capabilities": allowed_capabilities,
        "projects": allowed_projects,
        "devices": allowed_devices,
        "created_at": previous.get("created_at", now) if previous else now,
        "updated_at": now,
    }
    _atomic_write(_secret_path(handle), secret)
    _atomic_write(
        _meta_path(handle),
        json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return describe(handle)


def remove_local_secret(handle: str) -> None:
    handle = _handle(handle)
    for path in (_secret_path(handle), _meta_path(handle)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    leases = _load_leases()
    changed = False
    now = time.time()
    for lease in leases["leases"]:
        if lease.get("handle") == handle and lease.get("revoked_at") is None:
            lease["revoked_at"] = now
            lease["revoked_by"] = "credential_removed"
            changed = True
    if changed:
        _save_leases(leases)


def describe(handle: str) -> dict[str, Any]:
    handle = _handle(handle)
    meta = _load_meta(handle)
    secret_path = _secret_path(handle)
    present = secret_path.is_file()
    mode_ok = False
    if present:
        mode_ok = (secret_path.stat().st_mode & 0o077) == 0
    return {
        **meta,
        "secret_present": present,
        "secret_permissions_ok": mode_ok,
    }


def list_handles() -> list[dict[str, Any]]:
    root = credential_root()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        if path.name == "leases.json":
            continue
        handle = path.stem
        try:
            rows.append(describe(handle))
        except (OSError, ValueError, RuntimeError):
            continue
    rows.sort(key=lambda item: str(item.get("handle")))
    return rows


def create_lease(
    handle: str,
    *,
    capability: str,
    project: str | None = None,
    device: str | None = None,
    ttl_seconds: int = 300,
    issued_to: str = "mcp_model",
) -> dict[str, Any]:
    handle = _handle(handle)
    meta = describe(handle)
    if not meta["secret_present"] or not meta["secret_permissions_ok"]:
        raise PermissionError("credential secret is missing or has unsafe filesystem permissions")
    capability = str(capability).strip()
    if capability not in set(meta.get("capabilities") or []):
        raise PermissionError("credential does not allow the requested capability")
    project = None if project is None else str(project).strip()
    device = None if device is None else str(device).strip()
    allowed_projects = set(meta.get("projects") or [])
    allowed_devices = set(meta.get("devices") or [])
    if allowed_projects and project not in allowed_projects:
        raise PermissionError("credential is not scoped to the requested project")
    if allowed_devices and device not in allowed_devices:
        raise PermissionError("credential is not scoped to the requested device")
    ttl = min(900, max(30, int(ttl_seconds)))
    now = time.time()
    lease = {
        "lease_id": "credlease_" + uuid.uuid4().hex[:24],
        "handle": handle,
        "provider": meta.get("provider"),
        "capability": capability,
        "project": project,
        "device": device,
        "issued_to": str(issued_to)[:128],
        "issued_at": now,
        "expires_at": now + ttl,
        "revoked_at": None,
    }
    store = _load_leases()
    store["leases"].append(lease)
    _save_leases(store)
    return _public_lease(lease)


def list_leases(*, active_only: bool = True) -> list[dict[str, Any]]:
    now = time.time()
    rows = []
    for lease in _load_leases()["leases"]:
        active = lease.get("revoked_at") is None and float(lease.get("expires_at") or 0) > now
        if active_only and not active:
            continue
        row = _public_lease(lease)
        row["active"] = active
        rows.append(row)
    rows.sort(key=lambda item: float(item.get("issued_at") or 0), reverse=True)
    return rows


def revoke_lease(lease_id: str, *, operator: str = "mcp_operator") -> dict[str, Any]:
    store = _load_leases()
    lease = next((x for x in store["leases"] if x.get("lease_id") == lease_id), None)
    if lease is None:
        raise KeyError(lease_id)
    if lease.get("revoked_at") is None:
        lease["revoked_at"] = time.time()
        lease["revoked_by"] = str(operator)[:128]
        _save_leases(store)
    row = _public_lease(lease)
    row["active"] = False
    return row


def resolve_secret_for_lease(
    lease_id: str,
    *,
    capability: str,
    project: str | None = None,
    device: str | None = None,
) -> str:
    store = _load_leases()
    lease = next((x for x in store["leases"] if x.get("lease_id") == lease_id), None)
    if lease is None:
        raise KeyError(lease_id)
    now = time.time()
    if lease.get("revoked_at") is not None or float(lease.get("expires_at") or 0) <= now:
        raise PermissionError("credential lease is not active")
    if lease.get("capability") != capability:
        raise PermissionError("credential lease capability mismatch")
    if lease.get("project") != project or lease.get("device") != device:
        raise PermissionError("credential lease scope mismatch")
    path = _secret_path(str(lease["handle"]))
    if not path.is_file() or (path.stat().st_mode & 0o077) != 0:
        raise PermissionError("credential secret is missing or has unsafe filesystem permissions")
    secret = path.read_text(encoding="utf-8")
    if not secret:
        raise RuntimeError("credential secret is empty")
    return secret


def _load_meta(handle: str, *, missing_ok: bool = False) -> dict[str, Any]:
    path = _meta_path(handle)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    if not isinstance(value, dict) or int(value.get("version", 0)) != STORE_VERSION:
        raise RuntimeError("invalid credential metadata")
    if value.get("handle") != _handle(handle):
        raise RuntimeError("credential metadata identity mismatch")
    return value


def _load_leases() -> dict[str, Any]:
    path = _lease_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": STORE_VERSION, "leases": []}
    if not isinstance(value, dict) or int(value.get("version", 0)) != STORE_VERSION:
        raise RuntimeError("invalid credential lease store")
    leases = value.get("leases")
    if not isinstance(leases, list):
        raise RuntimeError("invalid credential lease store")
    return {"version": STORE_VERSION, "leases": leases}


def _save_leases(value: dict[str, Any]) -> None:
    _atomic_write(
        _lease_path(),
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _public_lease(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        key: lease.get(key)
        for key in (
            "lease_id", "handle", "provider", "capability", "project", "device",
            "issued_to", "issued_at", "expires_at", "revoked_at",
        )
    }

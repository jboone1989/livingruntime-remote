from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

STORE_VERSION = 1
MAX_CHECKPOINTS = 100
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
STATUSES = frozenset({
    "PENDING", "RUNNING", "STALLED", "WAITING", "BLOCKED",
    "SUCCEEDED", "FAILED", "CANCELLED", "CANCELLED_BY_USER",
})
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "CANCELLED_BY_USER"})


def jobs_root() -> Path:
    raw = os.environ.get("LIVINGRUNTIME_REMOTE_JOBS")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".livingruntime" / "jobs"


def _bounded(value: str | None, *, field: str, maximum: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    text = str(value).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} is too long")
    return text


def _validate_job_id(job_id: str) -> str:
    value = str(job_id).strip()
    if not JOB_ID_RE.fullmatch(value):
        raise ValueError("job_id must contain only letters, digits, dot, underscore, or dash")
    return value


def _path(job_id: str) -> Path:
    return jobs_root() / (_validate_job_id(job_id) + ".json")


def _save(value: dict[str, Any]) -> None:
    path = _path(str(value["job_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _load(job_id: str) -> dict[str, Any]:
    path = _path(job_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KeyError(job_id) from exc
    if not isinstance(value, dict) or int(value.get("version", 0)) != STORE_VERSION:
        raise RuntimeError("invalid LivingRuntime job state")
    if value.get("job_id") != _validate_job_id(job_id):
        raise RuntimeError("job state identity mismatch")
    return value


def create(
    *,
    goal: str,
    project: str | None = None,
    device: str | None = None,
    backend: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    goal = _bounded(goal, field="goal", maximum=8000, required=True) or ""
    project = _bounded(project, field="project", maximum=256)
    device = _bounded(device, field="device", maximum=256)
    normalized_backend = _normalize_backend(backend)
    initial = status or ("RUNNING" if normalized_backend is not None else "PENDING")
    if initial not in STATUSES:
        raise ValueError("unsupported job status")
    now = time.time()
    job_id = "lrjob_" + uuid.uuid4().hex[:20]
    value = {
        "version": STORE_VERSION,
        "job_id": job_id,
        "goal": goal,
        "status": initial,
        "terminal": initial in TERMINAL_STATUSES,
        "project": project,
        "device": device,
        "backend": normalized_backend,
        "current_step": None,
        "next_action": None,
        "runtime": {},
        "checkpoints": [],
        "created_at": now,
        "updated_at": now,
    }
    _save(value)
    return dict(value)


def get(job_id: str) -> dict[str, Any]:
    return dict(_load(job_id))


def list_jobs(*, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise ValueError("unsupported job status")
    bounded_limit = min(200, max(1, int(limit)))
    root = jobs_root()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or int(value.get("version", 0)) != STORE_VERSION:
            continue
        if status is not None and value.get("status") != status:
            continue
        rows.append(value)
    rows.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
    return [dict(item) for item in rows[:bounded_limit]]


def checkpoint(
    job_id: str,
    *,
    summary: str,
    current_step: str | None = None,
    next_action: str | None = None,
    status: str | None = None,
    source: str = "runtime",
) -> dict[str, Any]:
    value = _load(job_id)
    summary = _bounded(summary, field="summary", maximum=8000, required=True) or ""
    current_step = _bounded(current_step, field="current_step", maximum=2000)
    next_action = _bounded(next_action, field="next_action", maximum=4000)
    source = _bounded(source, field="source", maximum=128, required=True) or "runtime"
    if status is not None and status not in STATUSES:
        raise ValueError("unsupported job status")

    previous = str(value.get("status") or "PENDING")
    new_status = status or previous
    if previous in TERMINAL_STATUSES and new_status != previous:
        raise RuntimeError("terminal job status cannot transition")

    now = time.time()
    entry = {
        "seq": len(value.get("checkpoints") or []) + 1,
        "ts": now,
        "source": source,
        "status": new_status,
        "summary": summary,
        "current_step": current_step,
        "next_action": next_action,
    }
    checkpoints = list(value.get("checkpoints") or [])
    checkpoints.append(entry)
    value["checkpoints"] = checkpoints[-MAX_CHECKPOINTS:]
    value["status"] = new_status
    value["terminal"] = new_status in TERMINAL_STATUSES
    if current_step is not None:
        value["current_step"] = current_step
    if next_action is not None:
        value["next_action"] = next_action
    value["updated_at"] = now
    _save(value)
    return dict(value)


def update_runtime(
    job_id: str,
    *,
    runtime: dict[str, Any],
    status: str | None = None,
    current_step: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    """Persist lightweight liveness/progress without creating a checkpoint."""
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be an object")
    value = _load(job_id)
    previous = str(value.get("status") or "PENDING")
    new_status = status or previous
    if new_status not in STATUSES:
        raise ValueError("unsupported job status")
    if previous in TERMINAL_STATUSES and new_status != previous:
        raise RuntimeError("terminal job status cannot transition")

    allowed_text = {
        "backend_status": 64,
        "observed_status": 64,
        "progress_state": 64,
        "current_item": 2000,
        "error": 2000,
    }
    allowed_number = {
        "last_heartbeat_at",
        "last_progress_at",
        "heartbeat_age_seconds",
        "progress_age_seconds",
        "started_at",
        "finished_at",
        "stdout_bytes",
        "stderr_bytes",
        "pid",
        "child_pid",
        "returncode",
        "completed_items",
        "total_items",
    }
    allowed_bool = {"worker_alive", "child_alive"}
    normalized: dict[str, Any] = {}
    if "last_heartbeat_at" not in runtime and "heartbeat_at" in runtime:
        runtime = {**runtime, "last_heartbeat_at": runtime.get("heartbeat_at")}
    for key, maximum in allowed_text.items():
        raw = runtime.get(key)
        if raw is not None:
            normalized[key] = _bounded(
                str(raw), field=f"runtime.{key}", maximum=maximum
            )
    for key in allowed_number:
        raw = runtime.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"runtime.{key} must be numeric")
        normalized[key] = raw
    for key in allowed_bool:
        raw = runtime.get(key)
        if raw is None:
            continue
        if not isinstance(raw, bool):
            raise ValueError(f"runtime.{key} must be boolean")
        normalized[key] = raw

    current_step = _bounded(current_step, field="current_step", maximum=2000)
    next_action = _bounded(next_action, field="next_action", maximum=4000)
    value["runtime"] = normalized
    value["status"] = new_status
    value["terminal"] = new_status in TERMINAL_STATUSES
    if current_step is not None:
        value["current_step"] = current_step
    if next_action is not None:
        value["next_action"] = next_action
    value["updated_at"] = time.time()
    _save(value)
    return dict(value)


def find_by_backend(backend_type: str, backend_job_id: str) -> dict[str, Any] | None:
    backend_type = _bounded(backend_type, field="backend_type", maximum=64, required=True) or ""
    backend_job_id = _bounded(
        backend_job_id, field="backend_job_id", maximum=256, required=True
    ) or ""
    for value in list_jobs(limit=200):
        backend = value.get("backend")
        if not isinstance(backend, dict):
            continue
        if backend.get("type") == backend_type and backend.get("job_id") == backend_job_id:
            return value
    return None


def ensure_backend_job(
    *,
    backend_type: str,
    backend_job_id: str,
    goal: str | None = None,
    project: str | None = None,
    device: str | None = None,
    backend_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = find_by_backend(backend_type, backend_job_id)
    if existing is not None:
        return existing
    details = dict(backend_details or {})
    details["type"] = backend_type
    details["job_id"] = backend_job_id
    return create(
        goal=goal or f"{backend_type} job {backend_job_id}",
        project=project,
        device=device,
        backend=details,
        status="RUNNING",
    )


def attach_backend(
    job_id: str,
    backend: dict[str, Any],
    *,
    status: str = "RUNNING",
) -> dict[str, Any]:
    value = _load(job_id)
    if value.get("status") in TERMINAL_STATUSES:
        raise RuntimeError("terminal job cannot attach a new backend")
    if status not in STATUSES or status in TERMINAL_STATUSES:
        raise ValueError("attached backend status must be non-terminal")
    value["backend"] = _normalize_backend(backend)
    value["status"] = status
    value["terminal"] = False
    value["updated_at"] = time.time()
    _save(value)
    return dict(value)


def sync_backend_status(
    job_id: str,
    *,
    backend_status: str | None,
    summary: str | None = None,
) -> dict[str, Any]:
    value = _load(job_id)
    raw = str(backend_status or "").upper()
    mapped = {
        "PENDING": "PENDING",
        "QUEUED": "PENDING",
        "STARTING": "RUNNING",
        "STARTED": "RUNNING",
        "RUNNING": "RUNNING",
        "STALLED": "STALLED",
        "HEARTBEAT_STALE": "STALLED",
        "LOST": "STALLED",
        "WAITING": "WAITING",
        "BLOCKED": "BLOCKED",
        "SUCCEEDED": "SUCCEEDED",
        "SUCCESS": "SUCCEEDED",
        "COMPLETED": "SUCCEEDED",
        "FAILED": "FAILED",
        "ERROR": "FAILED",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
    }.get(raw)
    if mapped is None:
        return value
    if value.get("status") == mapped and not summary:
        return value
    return checkpoint(
        job_id,
        summary=summary or f"Backend status changed to {raw}.",
        status=mapped,
        source="backend",
    )


def _normalize_backend(backend: dict[str, Any] | None) -> dict[str, Any] | None:
    if backend is None:
        return None
    if not isinstance(backend, dict):
        raise ValueError("backend must be an object")
    backend_type = _bounded(
        backend.get("type"), field="backend.type", maximum=64, required=True
    )
    backend_job_id = _bounded(
        backend.get("job_id"), field="backend.job_id", maximum=256, required=True
    )
    normalized: dict[str, Any] = {
        "type": backend_type,
        "job_id": backend_job_id,
    }
    for key in (
        "pi_remote_dir",
        "job_root",
        "session_file",
        "session_id",
        "controller_mode",
        "provider",
        "model",
        "cwd",
        "executable",
    ):
        if key in backend and backend[key] is not None:
            normalized[key] = _bounded(
                str(backend[key]), field=f"backend.{key}", maximum=4096
            )
    return normalized

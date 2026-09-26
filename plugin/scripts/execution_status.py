from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

STATE_VERSION = 1
RECENT_ACTIVITY_SECONDS = 45.0
STALE_UI_SECONDS = 120.0

OBSERVABILITY_TOOLS = frozenset({
    "capabilities",
    "connection_status",
    "list_devices",
    "remote_overview",
    "list_projects",
    "list_jobs",
    "get_job",
    "get_long_job",
    "wait_long_job",
    "watch_long_job",
    "watch_pi_job",
    "wait_pi_job_completion",
    "watch_agent_cognition",
    "wait_llm_request",
    "get_llm_request",
    "get_llm_request_status",
    "list_exec_permissions",
    "list_credentials",
    "list_credential_leases",
    "diagnostics",
    "logs",
})

ACTIVE_JOB_STATES = frozenset({"PENDING", "RUNNING", "WAITING", "BLOCKED", "STALLED"})
COGNITION_PENDING_STATES = frozenset({"PENDING"})
COGNITION_CLAIMED_STATES = frozenset({"DISPATCHED", "CLAIMED"})
COGNITION_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED", "CANCELED", "EXPIRED"
})


def state_path() -> Path:
    override = os.environ.get("LIVINGRUNTIME_REMOTE_EXECUTION_STATE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".livingruntime" / "execution-state.json"


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_activity_at": None,
        "last_tool": None,
        "last_ok": None,
        "last_target": None,
        "last_job_id": None,
        "cognition": None,
    }


def _load() -> dict[str, Any]:
    path = state_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(value, dict) or int(value.get("version", 0)) != STATE_VERSION:
        return _empty_state()
    return {**_empty_state(), **value}


def _save(value: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _safe_target(tool: str, detail: dict[str, Any]) -> str | None:
    candidates: tuple[str, ...]
    if tool == "git":
        candidates = ("repo_path", "cwd", "project")
    elif tool in {"read_file", "write_file", "apply_patch", "list_dir"}:
        candidates = ("path", "project", "device")
    elif tool in {"start_long_job", "start_pi_agent", "start_pi_step"}:
        candidates = ("project", "device", "executable")
    elif tool == "exec":
        candidates = ("cwd", "project", "device")
    else:
        candidates = ("project", "device", "unit", "agent_id")
    for key in candidates:
        raw = detail.get(key)
        if raw is not None:
            text = str(raw).strip()
            if text:
                return text[:512]
    return None


def _is_observability_event(tool: str, detail: dict[str, Any]) -> bool:
    if tool in OBSERVABILITY_TOOLS:
        return True
    if tool == "process":
        return str(detail.get("action") or "").lower() == "list"
    if tool == "systemd":
        return str(detail.get("action") or "").lower() in {"status", "show"}
    return False


def record_tool_event(
    tool: str,
    ok: bool,
    detail: dict[str, Any] | None,
    *,
    observed_at: float | None = None,
) -> None:
    """Persist secret-free execution provenance after a real tool transition.

    Pure status/observation calls are intentionally ignored so opening or
    refreshing the dashboard cannot manufacture apparent work.
    """
    tool = str(tool or "").strip()
    if not tool:
        return
    detail = detail if isinstance(detail, dict) else {}
    now = float(observed_at if observed_at is not None else time.time())
    value = _load()

    if not _is_observability_event(tool, detail):
        value["last_activity_at"] = now
        value["last_tool"] = tool
        value["last_ok"] = bool(ok)
        value["last_target"] = _safe_target(tool, detail)

    job_id = detail.get("runtime_job_id") or detail.get("job_id")
    if job_id:
        value["last_job_id"] = str(job_id)[:256]

    if tool == "submit_llm_request":
        value["cognition"] = {
            "request_id": str(detail.get("request_id") or "")[:256] or None,
            "agent_id": str(detail.get("agent_id") or "")[:128] or None,
            "status": str(detail.get("status") or "PENDING").upper(),
            "updated_at": now,
        }
    elif tool == "claim_llm_request":
        cognition = value.get("cognition")
        if not isinstance(cognition, dict):
            cognition = {}
        cognition.update({
            "request_id": str(detail.get("request_id") or cognition.get("request_id") or "")[:256] or None,
            "status": str(detail.get("status") or "DISPATCHED").upper(),
            "updated_at": now,
        })
        value["cognition"] = cognition
    elif tool == "complete_llm_request":
        cognition = value.get("cognition")
        if not isinstance(cognition, dict):
            cognition = {}
        cognition.update({
            "request_id": str(detail.get("request_id") or cognition.get("request_id") or "")[:256] or None,
            "status": str(detail.get("status") or "COMPLETED").upper(),
            "updated_at": now,
        })
        value["cognition"] = cognition

    _save(value)


def tracked_cognition_request_id() -> str | None:
    cognition = _load().get("cognition")
    if not isinstance(cognition, dict):
        return None
    request_id = str(cognition.get("request_id") or "").strip()
    return request_id or None


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    runtime = job.get("runtime") if isinstance(job.get("runtime"), dict) else {}
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "goal": str(job.get("goal") or "")[:300],
        "current_step": str(job.get("current_step") or "")[:300] or None,
        "next_action": str(job.get("next_action") or "")[:300] or None,
        "worker_alive": bool(runtime.get("worker_alive")),
        "child_alive": bool(runtime.get("child_alive")),
        "heartbeat_age_seconds": runtime.get("heartbeat_age_seconds"),
        "progress_age_seconds": runtime.get("progress_age_seconds"),
    }


def snapshot(
    *,
    jobs: list[dict[str, Any]],
    cognition_status: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a conservative source-of-truth execution state."""
    now = float(now if now is not None else time.time())
    value = _load()
    active_jobs = [
        row for row in jobs
        if isinstance(row, dict)
        and not bool(row.get("terminal"))
        and str(row.get("status") or "").upper() in ACTIVE_JOB_STATES
    ]
    summaries = [_job_summary(row) for row in active_jobs[:8]]
    workers_alive = sum(
        1 for row in summaries if row["worker_alive"] or row["child_alive"]
    )

    cognition = value.get("cognition")
    if not isinstance(cognition, dict):
        cognition = None
    if isinstance(cognition_status, dict):
        observed = str(cognition_status.get("status") or "").upper()
        if cognition is None:
            cognition = {}
        cognition = {
            **cognition,
            "request_id": cognition_status.get("request_id") or cognition.get("request_id"),
            "agent_id": cognition_status.get("agent_id") or cognition.get("agent_id"),
            "status": observed or cognition.get("status"),
            "updated_at": cognition_status.get("updated_at")
            or cognition_status.get("completed_at")
            or cognition.get("updated_at"),
        }

    cognition_state = str((cognition or {}).get("status") or "").upper()
    last_activity_at = value.get("last_activity_at")
    age = None
    if isinstance(last_activity_at, (int, float)):
        age = max(0.0, now - float(last_activity_at))

    job_states = {str(row.get("status") or "").upper() for row in active_jobs}
    metadata_only_running = any(
        row["status"] in {"RUNNING", "PENDING"}
        and not (row["worker_alive"] or row["child_alive"])
        for row in summaries
    )
    if "STALLED" in job_states:
        state = "STALLED"
        message = "A durable job is stalled; inspect its heartbeat/progress receipt."
    elif workers_alive:
        state = "RUNNING"
        message = "A server-side worker or child process is alive."
    elif cognition_state in COGNITION_PENDING_STATES:
        state = "WAITING_FOR_COGNITION"
        message = "An agent cognition request exists and is waiting for ChatGPT to claim it."
    elif cognition_state in COGNITION_CLAIMED_STATES:
        state = "CLAIMED_BY_CHATGPT"
        message = "A cognition request has been claimed and is waiting for the ChatGPT response."
    elif "BLOCKED" in job_states:
        state = "BLOCKED"
        message = "A durable job is blocked on an external condition or approval."
    elif "WAITING" in job_states:
        state = "WAITING"
        message = "A durable job is waiting; no worker is currently executing."
    elif metadata_only_running:
        state = "WAITING"
        message = (
            "A durable job is marked running/pending, but no live server process is "
            "currently observed. Treat the receipt as waiting until liveness returns."
        )
    elif age is not None and age <= RECENT_ACTIVITY_SECONDS:
        state = "RECENT_ACTIVITY"
        message = "A real Remote tool completed recently; no durable worker is currently running."
    else:
        state = "IDLE"
        message = "No server-side durable work or cognition wait is currently running."

    if cognition_state in COGNITION_TERMINAL_STATES:
        cognition = None

    ui_may_be_stale = (
        state == "IDLE"
        and age is not None
        and age >= STALE_UI_SECONDS
    )
    return {
        "schema_version": "livingruntime.execution-status.v1",
        "source_of_truth": "server_receipt",
        "generated_at": now,
        "state": state,
        "message": message,
        "active_job_count": len(active_jobs),
        "worker_alive_count": workers_alive,
        "running_requires_live_process": True,
        "active_jobs": summaries,
        "last_real_activity_at": last_activity_at,
        "last_real_activity_age_seconds": None if age is None else round(age, 3),
        "last_tool": value.get("last_tool"),
        "last_tool_ok": value.get("last_ok"),
        "last_target": value.get("last_target"),
        "last_job_id": value.get("last_job_id"),
        "cognition": cognition,
        "ui_may_be_stale": ui_may_be_stale,
        "ui_warning": (
            "No server-side work is running. If ChatGPT still shows an old tool as running, that UI timeline is stale."
            if ui_may_be_stale
            else None
        ),
    }

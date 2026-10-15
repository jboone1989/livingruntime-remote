"""Isolated durable-session event inbox (no autonomous execution)."""
import json
import os
import re
import time
import uuid
from pathlib import Path

IDENT = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

def _path(session_id):
    if not IDENT.fullmatch(session_id):
        raise ValueError("invalid session id")
    root = Path(os.environ.get("LIVINGRUNTIME_REMOTE_SESSIONS", str(Path.home() / ".livingruntime" / "sessions")))
    return root / (session_id + ".json")

def _save(state):
    path = _path(state["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("x", encoding="utf-8") as fh:
        os.chmod(tmp, 0o600)
        json.dump(state, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

def create(goal, *, device, project):
    if not goal.strip() or len(goal) > 8000 or not device or not project:
        raise ValueError("invalid session parameters")
    state = {"session_id": "lrs_" + uuid.uuid4().hex, "goal": goal,
             "device": device, "project": project, "status": "ACTIVE",
             "events": [], "created_at": time.time()}
    _save(state)
    return state

def get(session_id):
    return json.loads(_path(session_id).read_text(encoding="utf-8"))

def receive(session_id, *, event_id, job_id, status):
    if not IDENT.fullmatch(event_id) or not IDENT.fullmatch(job_id):
        raise ValueError("invalid event identity")
    if status not in ("SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"):
        raise ValueError("invalid event status")
    state = get(session_id)
    prior = next((e for e in state["events"] if e["event_id"] == event_id), None)
    if prior is not None:
        if prior["job_id"] != job_id or prior["status"] != status:
            raise ValueError("conflicting event replay")
        return state
    state["events"].append({"event_id": event_id, "job_id": job_id,
                            "status": status, "handled": False})
    _save(state)
    return state

def acknowledge(session_id, event_id):
    state = get(session_id)
    event = next((e for e in state["events"] if e["event_id"] == event_id), None)
    if event is None:
        raise KeyError(event_id)
    event["handled"] = True
    _save(state)
    return state

def pending(session_id):
    return [dict(e) for e in get(session_id)["events"] if not e["handled"]]

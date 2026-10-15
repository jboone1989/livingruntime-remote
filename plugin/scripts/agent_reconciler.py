"""Bounded one-shot reconciliation of active adapter sessions into durable Jobs."""
from __future__ import annotations
import json
import os
from pathlib import Path

def reconcile(limit=100):
    import agent_sessions
    from agent_adapters import load_installed, registry
    if not registry.list_agents():
        load_installed()
    root=Path(os.environ.get("LIVINGRUNTIME_AGENT_SESSIONS",
        str(Path.home()/".livingruntime"/"agent-sessions")))
    if not root.exists():
        return {"checked":0,"updated":0,"errors":0}
    checked=updated=errors=0
    failures=[]
    candidates=sorted(root.glob("lra_*.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in candidates[:max(1,min(int(limit),500))]:
        try:
            record=json.loads(path.read_text(encoding="utf-8"))
            job_id=record.get("runtime_job_id")
            if not job_id:
                continue
            from jobs import get
            before=get(job_id)
            if before.get("terminal"):
                continue
            checked+=1
            agent_sessions.status(record["session_id"])
            after=get(job_id)
            if after.get("status")!=before.get("status"):
                updated+=1
        except (OSError,KeyError,ValueError,RuntimeError,PermissionError) as exc:
            errors+=1
            if len(failures)<10:
                failures.append({"session_id":path.stem,"error_type":type(exc).__name__})
    return {"checked":checked,"updated":updated,"errors":errors,"failures":failures}

if __name__=="__main__":
    print(json.dumps(reconcile(),sort_keys=True))

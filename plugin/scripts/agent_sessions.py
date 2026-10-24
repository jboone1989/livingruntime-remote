"""Persist adapter session ownership; never infer authority from a client session ID."""
from __future__ import annotations
import json
import os
import re
import uuid
import time
from pathlib import Path
from agent_adapters import registry
import jobs

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
def _path(sid):
    if not _ID.fullmatch(sid):
        raise ValueError("invalid session id")
    root = Path(os.environ.get("LIVINGRUNTIME_AGENT_SESSIONS",str(Path.home()/".livingruntime"/"agent-sessions")))
    return root/(sid+".json")
def _save(record):
    path=_path(record["session_id"])
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    with tmp.open("x",encoding="utf-8") as f:
        os.chmod(tmp,0o600)
        json.dump(record,f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,path)
def get(sid):
    return json.loads(_path(sid).read_text(encoding="utf-8"))
def start(adapter_id,goal,*,project,device,authorized=False,options=None):
    if not goal.strip() or len(goal)>8000 or not project or not device:
        raise ValueError("invalid agent start arguments")
    if authorized is not True:
        raise PermissionError("agent launch requires explicit authorization")
    adapter=registry.get(adapter_id)
    if options is not None and adapter_id not in {"claude-code", "codex"}:
        raise ValueError("CLI options are only supported by registered native CLI adapters")
    kwargs={"project":project,"device":device}
    if options is not None:
        kwargs["options"]=options
    external=adapter.start(goal,**kwargs)
    if not isinstance(external,str) or not external or len(external)>256:
        raise ValueError("adapter returned invalid session")
    record={"session_id":"lra_"+uuid.uuid4().hex,"adapter_id":adapter_id,
            "external_session_id":external,"project":project,"device":device}
    job = jobs.create(goal=goal,project=project,device=device,backend={"type":"agent-adapter","job_id":record["session_id"],"session_id":record["session_id"]})
    record["runtime_job_id"] = job["job_id"]
    record["state"] = "ACTIVE"
    record["created_at"] = time.time()
    _save(record)
    return {"session_id":record["session_id"],"adapter_id":adapter_id,"project":project,"device":device,"runtime_job_id":job["job_id"]}
def _resolve(sid,adapter_id=None):
    record=get(sid)
    if adapter_id is not None and record["adapter_id"]!=adapter_id:
        raise PermissionError("session belongs to another adapter")
    return record,registry.get(record["adapter_id"])
def status(sid):
    record,adapter=_resolve(sid)
    observed = adapter.status(record["external_session_id"])
    if not isinstance(observed,dict):
        raise ValueError("adapter status must be an object")
    native = str(observed.get("status","UNKNOWN")).upper()
    if record.get("state") == "CANCELLED":
        native = "CANCELLED"
        observed = {**observed, "status": "CANCELLED"}
    terminal = {"SUCCEEDED","FAILED","CANCELLED"}
    if native in terminal and record.get("runtime_job_id"):
        job = jobs.get(record["runtime_job_id"])
        if not job.get("terminal"):
            jobs.checkpoint(job["job_id"],summary="Agent reported "+native,status=native)
    return {"session_id":sid,"runtime_job_id":record.get("runtime_job_id"),"agent_status":observed}
def send(sid,message):
    record,adapter=_resolve(sid)
    if record.get("state") == "CANCELLED":
        raise RuntimeError("agent session cancelled")
    if not adapter.capabilities().messaging:
        raise NotImplementedError("adapter does not support messaging")
    if not message.strip() or len(message)>8000:
        raise ValueError("invalid message")
    adapter.send(record["external_session_id"],message)
    previous=jobs.get(record["runtime_job_id"]) if record.get("runtime_job_id") else None
    if previous is not None and previous.get("terminal"):
        next_job=jobs.create(goal=message,project=record["project"],device=record["device"],
            backend={"type":"agent-adapter","job_id":sid,"session_id":sid})
        record["runtime_job_id"]=next_job["job_id"]
        record["updated_at"]=time.time()
        _save(record)
    return {"session_id":sid,"accepted":True,"runtime_job_id":record.get("runtime_job_id")}
def cancel(sid):
    record,adapter=_resolve(sid)
    if record.get("state") == "CANCELLED":
        return {"session_id":sid,"cancel_requested":False,"already_cancelled":True}
    if not adapter.capabilities().cancel:
        raise NotImplementedError("adapter does not support cancellation")
    adapter.cancel(record["external_session_id"])
    record["state"] = "CANCELLED"
    record["updated_at"] = time.time()
    _save(record)
    if record.get("runtime_job_id"):
        job = jobs.get(record["runtime_job_id"])
        if not job.get("terminal"):
            jobs.checkpoint(job["job_id"],summary="Agent session cancelled by operator",status="CANCELLED")
    return {"session_id":sid,"cancel_requested":True}

def resume(sid):
    record,adapter=_resolve(sid)
    if record.get("state") == "CANCELLED":
        raise RuntimeError("cancelled agent session cannot resume")
    if not adapter.capabilities().resume:
        raise NotImplementedError("adapter does not support resume")
    result=adapter.resume(record["external_session_id"])
    if not isinstance(result,dict):
        raise ValueError("adapter resume must return an object")
    return {"session_id":sid,"agent_status":result}

def decide(sid,request_id,decision,answers=None):
    record,adapter=_resolve(sid,"claude-code")
    if record.get("state")=="CANCELLED":
        raise RuntimeError("cancelled session")
    return adapter.decide(record["external_session_id"],request_id,decision,answers=answers)

def events(sid,after=0):
    record,adapter=_resolve(sid)
    if not adapter.capabilities().events:
        raise NotImplementedError("adapter does not support events")
    if not isinstance(after,int) or after<0:
        raise ValueError("invalid event offset")
    result=adapter.events(record["external_session_id"],after=after)
    if not isinstance(result,list) or len(result)>100:
        raise ValueError("adapter event result exceeds limit")
    return {"session_id":sid,"events":result,"next_offset":result[-1].get("seq",after+len(result)) if result else after}

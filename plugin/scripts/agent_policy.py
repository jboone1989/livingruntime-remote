"""Operator-owned, fail-closed Agent Adapter policy."""
from __future__ import annotations
import json
import os
import stat
from pathlib import Path

def policy_path():
    return Path(os.environ.get("LIVINGRUNTIME_AGENT_POLICY",str(Path.home()/".livingruntime"/"agent-policy.json")))

def load():
    path=policy_path()
    if not path.exists():
        return {}
    meta=path.stat()
    if meta.st_uid!=os.getuid() or stat.S_IMODE(meta.st_mode)&0o077:
        raise PermissionError("agent policy must be owner-only")
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict):
        raise ValueError("agent policy must be object")
    return value

def may_launch(device,project,adapter):
    key=f"{device}:{project}:{adapter}"
    env_entries=os.environ.get("LIVINGRUNTIME_AGENT_LAUNCH_ALLOWLIST","").split(",")
    entries=load().get("launch_allowlist",[])
    if not isinstance(entries,list) or not all(isinstance(x,str) for x in entries):
        raise ValueError("invalid launch allowlist")
    return key in entries or key in env_entries

def claude_workspace(device,project):
    key=f"{device}:{project}"
    policy=load().get("claude_workspaces",{})
    if not isinstance(policy,dict):
        raise ValueError("invalid workspace mapping")
    env_raw=json.loads(os.environ.get("LIVINGRUNTIME_CLAUDE_WORKSPACES","{}"))
    if not isinstance(env_raw,dict):
        raise ValueError("invalid environment workspace map")
    value=env_raw.get(key,policy.get(key))
    if not isinstance(value,str) or not Path(value).is_dir():
        raise PermissionError("Claude project workspace not allowlisted")
    return str(Path(value).resolve())

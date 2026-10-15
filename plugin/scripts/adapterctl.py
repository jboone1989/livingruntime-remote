"""Operator-only CLI for explicit adapter manifest registration."""
from __future__ import annotations
import argparse
import json
import os
import re
import uuid
from pathlib import Path

def location() -> Path:
    return Path(os.environ.get("LIVINGRUNTIME_AGENT_ADAPTERS",str(Path.home()/".livingruntime"/"agent-adapters.json")))
def load(path: Path) -> dict:
    if not path.exists():
        return {"adapters":[]}
    data=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data,dict) or not isinstance(data.get("adapters"),list):
        raise ValueError("invalid adapter manifest")
    return data
def save(path: Path,data: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    with temp.open("x",encoding="utf-8") as out:
        os.chmod(temp,0o600)
        json.dump(data,out,indent=2,sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp,path)
def change(action: str,module: str|None=None,factory: str|None=None,path: Path|None=None) -> dict:
    path=path or location()
    data=load(path)
    if action=="list":
        return data
    if not isinstance(module,str) or not re.fullmatch(r"livingruntime_adapter_[a-zA-Z0-9_]+",module):
        raise ValueError("module must be an explicit adapter namespace")
    rows=data["adapters"]
    if action=="install":
        if not isinstance(factory,str) or not factory.isidentifier():
            raise ValueError("invalid factory")
        if any(row.get("module")==module for row in rows):
            raise ValueError("adapter already installed")
        rows.append({"module":module,"factory":factory})
    elif action=="uninstall":
        if not any(row.get("module")==module for row in rows):
            raise KeyError("adapter not installed")
        data["adapters"]=[row for row in rows if row.get("module")!=module]
    else:
        raise ValueError("unknown operation")
    save(path,data)
    return data
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("list","install","uninstall"))
    parser.add_argument("module",nargs="?")
    parser.add_argument("factory",nargs="?")
    args=parser.parse_args()
    print(json.dumps(change(args.action,args.module,args.factory),sort_keys=True))
if __name__=="__main__":
    main()

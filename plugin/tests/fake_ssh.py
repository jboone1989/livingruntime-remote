#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(os.environ.get("FAKE_REMOTE_ROOT", ".")).resolve()
UNIT = os.environ.get("FAKE_REMOTE_UNIT", "content-agent.service")


def identity() -> dict[str, str]:
    return {"user": "ubuntu", "hostname": "livingruntime-host", "cwd": str(ROOT)}


def handle_agent(payload: dict) -> dict:
    op = payload["op"]
    if op == "read_file":
        path = ROOT / Path(payload["path"]).name
        data = path.read_bytes()[payload.get("offset", 0):][: payload.get("max_bytes", 131072)]
        return {
            "path": str(path),
            "offset": payload.get("offset", 0),
            "bytes_read": len(data),
            "content": data.decode("utf-8", errors="replace"),
            "sha256": "abc",
        }
    if op == "exec":
        argv = payload.get("argv") or []
        cwd = payload.get("cwd")
        if argv[:1] == ["git"] and "status" in argv:
            return {"returncode": 0, "stdout": "On branch main\nnothing to commit\n", "stderr": "", "cwd": cwd}
        return {"returncode": 0, "stdout": "ok\n", "stderr": "", "cwd": cwd}
    raise ValueError(f"unsupported fake op {op}")


def main() -> None:
    command = sys.argv[-1] if sys.argv[1:] else ""
    if "getpass" in command or "socket.gethostname" in command:
        sys.stdout.write(json.dumps(identity()))
        return
    raw = sys.stdin.read()
    if raw.strip().startswith("{"):
        sys.stdout.write(json.dumps(handle_agent(json.loads(raw))))
        return
    if command.startswith("journalctl") or "journalctl" in command:
        if UNIT not in command:
            sys.stderr.write("unit is not allowlisted\n")
            raise SystemExit(1)
        sys.stdout.write("Sep 20 00:00:00 host content-agent[1]: ready\n")
        return
    sys.stdout.write(json.dumps(identity()))


if __name__ == "__main__":
    main()

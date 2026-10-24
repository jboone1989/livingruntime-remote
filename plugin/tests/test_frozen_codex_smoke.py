"""Smoke-test a genuinely frozen connector running a native Codex JSONL worker."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: test_frozen_codex_smoke.py CONNECTOR_BINARY")
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        codex = root / "fake-codex"
        thread = str(uuid.uuid4())
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json,sys\n"
            "assert sys.argv[1:4]==['exec','--json','--cd'],sys.argv\n"
            f"print(json.dumps({{'type':'thread.started','thread_id':'{thread}'}}),flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':"
            "{'id':'msg_1','type':'agent_message','text':'FROZEN_CODEX_OK'}}),flush=True)\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':3}}),flush=True)\n",
            encoding="utf-8",
        )
        codex.chmod(0o700)
        jobs = root / "jobs"
        jobs.mkdir()
        sid = str(uuid.uuid4())
        record = jobs / (sid + ".json")
        record.write_text(json.dumps({
            "session_id": sid, "cwd": str(root), "executable": str(codex),
            "prompt": "test", "continue": False, "options": {"sandbox": "read-only"},
        }), encoding="utf-8")
        env = {**os.environ, "LIVINGRUNTIME_CODEX_JOBS": str(jobs)}
        proc = subprocess.run([binary, "agent-worker", "codex", str(record)],
                              env=env, capture_output=True, text=True, timeout=45)
        if proc.returncode:
            raise AssertionError(f"Packaged worker failed: {proc.returncode}: {proc.stderr[-1000:]}")
        receipt = json.loads(record.with_suffix(".result.json").read_text())
        assert receipt["status"] == "SUCCEEDED", receipt
        assert receipt["result"] == "FROZEN_CODEX_OK", receipt
        events = [json.loads(line) for line in record.with_suffix(".events.jsonl").read_text().splitlines()]
        assert events[-1]["kind"] == "completion" and events[-1]["success"], events
        print("FROZEN_CODEX_OK")


if __name__ == "__main__":
    main()

"""Detached bounded Claude executor. Operator allowlists are checked by adapter."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path

def main(path: str) -> None:
    record_path=Path(path)
    receipt_path=record_path.with_suffix(".result.json")
    record=json.loads(record_path.read_text(encoding="utf-8"))
    args=[record["executable"],"-p",record["prompt"],"--output-format","json",
          "--permission-mode","plan","--max-turns","1"]
    if record.get("continue"):
        args += ["--resume",record["session_id"]]
    else:
        args += ["--session-id",record["session_id"]]
    output={"status":"FAILED","finished_at":time.time()}
    try:
        done=subprocess.run(args,cwd=record["cwd"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            text=True,timeout=300,check=False)
        if done.returncode:
            output["error"]="Claude process returned nonzero exit status"
        else:
            result=json.loads(done.stdout[-131072:])
            output["status"]="FAILED" if result.get("is_error") else "SUCCEEDED"
            output["result"]=str(result.get("result",""))[:8192]
    except subprocess.TimeoutExpired:
        output["status"]="STALLED"
        output["error"]="Claude timed out"
    except (ValueError, OSError) as exc:
        output["error"]=type(exc).__name__ + ": " + str(exc)[:240]
    output["finished_at"]=time.time()
    tmp=receipt_path.with_suffix(".tmp")
    with tmp.open("w",encoding="utf-8") as f:
        os.chmod(tmp,0o600)
        json.dump(output,f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,receipt_path)

if __name__=="__main__":
    if len(sys.argv)!=2: raise SystemExit(2)
    main(sys.argv[1])

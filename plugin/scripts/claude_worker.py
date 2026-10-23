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
          "--permission-mode","plan","--max-turns","10"]
    if record.get("continue"):
        args += ["--resume",record["session_id"]]
    else:
        args += ["--session-id",record["session_id"]]
    output={"status":"FAILED","finished_at":time.time()}
    try:
        done=subprocess.run(args,cwd=record["cwd"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            text=True,timeout=300,check=False)
        result=None
        if done.stdout.strip() and len(done.stdout)<=2_000_000:
            try:
                result=json.loads(done.stdout)
            except ValueError:
                pass
        if done.returncode:
            detail=(result.get("result") if isinstance(result,dict) else None) or done.stderr.strip() or "no JSON result"
            output["error"]=f"Claude exit={done.returncode}: {str(detail)[-800:]}"
        elif not isinstance(result,dict):
            output["error"]="Claude returned no valid JSON result object"
        elif result.get("is_error") or str(result.get("subtype","")).startswith("error_"):
            detail=result.get("result") or result.get("subtype") or "unknown error"
            output["error"]=f"Claude exit=0: {str(detail)[-800:]}"
        elif not isinstance(result.get("result"),str) or not result["result"].strip():
            output["error"]="Claude returned no result text"
        else:
            output["status"]="SUCCEEDED"
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

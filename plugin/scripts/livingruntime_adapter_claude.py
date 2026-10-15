"""Optional supervised Claude Code adapter, disabled without explicit operator allowlists."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from agent_adapters import AgentCapabilities
from agent_policy import claude_workspace

class ClaudeCodeAdapter:
    adapter_id="claude-code"
    def __init__(self,executable="/home/ubuntu/.local/bin/claude"):
        path=Path(executable)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError("Claude Code executable unavailable")
        self.executable=str(path)
        self.root=Path(os.environ.get("LIVINGRUNTIME_CLAUDE_JOBS",str(Path.home()/".livingruntime"/"claude-jobs")))
        self.worker=Path(__file__).with_name("claude_worker.py")

    def capabilities(self):
        return AgentCapabilities(resume=True,messaging=True,events=False,cancel=True)

    def _path(self,sid):
        if not isinstance(sid,str) or len(sid)!=36 or any(c not in "0123456789abcdef-" for c in sid):
            raise ValueError("invalid Claude session id")
        return self.root/(sid+".json")

    def _read(self,sid):
        return json.loads(self._path(sid).read_text(encoding="utf-8"))

    def _write(self,sid,data):
        path=self._path(sid)
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        temp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
        with temp.open("x",encoding="utf-8") as f:
            os.chmod(temp,0o600)
            json.dump(data,f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp,path)

    def _spawn(self,sid,prompt,continued):
        record=self._read(sid)
        if record.get("status")=="RUNNING":
            raise RuntimeError("Claude session already running")
        record.update({"status":"RUNNING","prompt":prompt,"continue":continued,"started_at":time.time()})
        self._write(sid,record)
        receipt=self._path(sid).with_suffix(".result.json")
        if receipt.exists():
            receipt.unlink()
        proc=subprocess.Popen([sys.executable,str(self.worker),str(self._path(sid))],
                              stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                              start_new_session=True,close_fds=True)
        record["pid"]=proc.pid
        self._write(sid,record)
        return sid

    def start(self,goal,*,project,device):
        if not isinstance(goal,str) or not goal.strip() or len(goal)>8000:
            raise ValueError("invalid goal")
        cwd=claude_workspace(device,project)
        sid=str(uuid.uuid4())
        self._write(sid,{"session_id":sid,"cwd":str(Path(cwd).resolve()),
                         "executable":self.executable,"status":"PENDING","project":project,"device":device})
        return self._spawn(sid,goal,False)

    def _worker_alive(self,record):
        pid=record.get("pid")
        if not isinstance(pid,int) or pid<=1:
            return False
        try:
            cmd=(Path("/proc")/str(pid)/"cmdline").read_bytes()
            return b"claude_worker.py" in cmd and str(self._path(record["session_id"])).encode() in cmd
        except OSError:
            return False

    def status(self,sid):
        record=self._read(sid)
        receipt=self._path(sid).with_suffix(".result.json")
        if record["status"]=="RUNNING" and receipt.exists():
            report=json.loads(receipt.read_text(encoding="utf-8"))
            record.update({"status":report["status"],"result":report.get("result",""),
                           "error":report.get("error"),"finished_at":report["finished_at"]})
            self._write(sid,record)
        elif record["status"]=="RUNNING" and not self._worker_alive(record):
            if time.time()-record.get("started_at",0)>5:
                record["status"]="STALLED"
                record["error"]="Worker not running and no completion receipt"
                self._write(sid,record)
        return {"status":record["status"],"result":record.get("result",""),
                "error":record.get("error"),"started_at":record.get("started_at"),
                "finished_at":record.get("finished_at")}

    def send(self,sid,message):
        status=self.status(sid)["status"]
        if status=="RUNNING":
            raise RuntimeError("Claude session is busy")
        if status=="CANCELLED":
            raise RuntimeError("Claude session cancelled")
        if not isinstance(message,str) or not message.strip() or len(message)>8000:
            raise ValueError("invalid message")
        self._spawn(sid,message,True)

    def resume(self,sid):
        return self.status(sid)

    def cancel(self,sid):
        record=self._read(sid)
        if record["status"]=="RUNNING" and self._worker_alive(record):
            os.killpg(record["pid"],signal.SIGTERM)
        record["status"]="CANCELLED"
        self._write(sid,record)

def make():
    return ClaudeCodeAdapter()

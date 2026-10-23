"""Native Claude Code CLI adapter for LivingRuntime Remote.

The CLI owns agents, skills, hooks, tools, MCP, and session context.
Remote owns durable sessions, workspace policy, observable events, and approvals.
"""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from agent_adapters import AgentCapabilities
from agent_policy import claude_workspace

_ALLOWED_OPTIONS = {
    "agent", "agents", "model", "effort", "permission_mode", "max_turns",
    "allowed_tools", "disallowed_tools", "tools", "append_system_prompt",
    "duplex", "timeout_seconds",
}
_MODES = {"plan", "default", "acceptEdits", "auto"}
_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultracode"}
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_TOOL = re.compile(r"^[A-Za-z_][^,\r\n\x00]{0,199}$")


def validate_options(value):
    """Allow native CLI features without accepting arbitrary shell/CLI flags."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _ALLOWED_OPTIONS:
        raise ValueError("unsupported Claude CLI option")
    opts = dict(value)
    for key in ("agent", "model"):
        if key in opts and (not isinstance(opts[key], str) or not _IDENT.fullmatch(opts[key])):
            raise ValueError("invalid " + key)
    if "effort" in opts and opts["effort"] not in _EFFORTS:
        raise ValueError("invalid effort")
    if "permission_mode" in opts and opts["permission_mode"] not in _MODES:
        raise ValueError("invalid permission mode")
    if "max_turns" in opts and (type(opts["max_turns"]) is not int or not 1 <= opts["max_turns"] <= 300):
        raise ValueError("invalid max_turns")
    if "timeout_seconds" in opts and (type(opts["timeout_seconds"]) is not int or not 30 <= opts["timeout_seconds"] <= 3600):
        raise ValueError("invalid timeout_seconds")
    if "duplex" in opts and type(opts["duplex"]) is not bool:
        raise ValueError("duplex must be boolean")
    for key in ("allowed_tools", "disallowed_tools", "tools"):
        if key in opts:
            arr = opts[key]
            if not isinstance(arr, list) or len(arr) > 50 or not all(
                isinstance(x, str) and _TOOL.fullmatch(x) for x in arr
            ):
                raise ValueError("invalid " + key)
    if "agents" in opts:
        agents = opts["agents"]
        if not isinstance(agents, dict) or not agents or len(agents) > 12 or len(json.dumps(agents)) > 30000:
            raise ValueError("invalid dynamic agents")
        if not all(isinstance(k, str) and _IDENT.fullmatch(k) and isinstance(v, dict)
                   and isinstance(v.get("prompt"), str) and isinstance(v.get("description"), str)
                   for k, v in agents.items()):
            raise ValueError("invalid agent definitions")
    if "append_system_prompt" in opts and (
        not isinstance(opts["append_system_prompt"], str)
        or len(opts["append_system_prompt"]) > 20000
    ):
        raise ValueError("invalid append_system_prompt")
    return opts


class ClaudeCodeAdapter:
    adapter_id = "claude-code"

    def __init__(self, executable="/home/ubuntu/.local/bin/claude"):
        path = Path(executable)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError("Claude Code executable unavailable")
        self.executable = str(path)
        self.root = Path(os.environ.get("LIVINGRUNTIME_CLAUDE_JOBS", str(Path.home() / ".livingruntime" / "claude-jobs")))
        self.worker = Path(__file__).with_name("claude_worker.py")

    def capabilities(self):
        return AgentCapabilities(resume=True, messaging=True, events=True, cancel=True)

    def _path(self, sid):
        try:
            parsed = uuid.UUID(sid)
            if str(parsed) != sid:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("invalid Claude session id")
        return self.root / (sid + ".json")

    def _read(self, sid):
        return json.loads(self._path(sid).read_text(encoding="utf-8"))

    def _write(self, sid, data):
        path = self._path(sid)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        with temp.open("x", encoding="utf-8") as f:
            os.chmod(temp, 0o600)
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)

    def _spawn(self, sid, prompt, continued):
        record = self._read(sid)
        if self.status(sid)["status"] == "RUNNING":
            raise RuntimeError("Claude session already running")
        record.update({"status": "RUNNING", "prompt": prompt, "continue": continued, "started_at": time.time()})
        self._write(sid, record)
        for suffix in (".result.json", ".events.jsonl", ".decisions.jsonl"):
            path = self._path(sid).with_suffix(suffix)
            if path.exists():
                path.unlink()
        proc = subprocess.Popen(
            [sys.executable, str(self.worker), str(self._path(sid))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        record["pid"] = proc.pid
        self._write(sid, record)
        return sid

    def start(self, goal, *, project, device, options=None):
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 40000:
            raise ValueError("invalid goal")
        cwd = claude_workspace(device, project)
        sid = str(uuid.uuid4())
        self._write(sid, {"session_id": sid, "cwd": str(Path(cwd).resolve()),
                          "executable": self.executable, "status": "PENDING",
                          "project": project, "device": device, "options": validate_options(options)})
        return self._spawn(sid, goal, False)

    def _worker_alive(self, record):
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            return False
        try:
            cmd = (Path("/proc") / str(pid) / "cmdline").read_bytes()
            return b"claude_worker.py" in cmd and str(self._path(record["session_id"])).encode() in cmd
        except OSError:
            return False

    def status(self, sid):
        record = self._read(sid)
        receipt = self._path(sid).with_suffix(".result.json")
        if record["status"] == "RUNNING" and receipt.exists():
            report = json.loads(receipt.read_text(encoding="utf-8"))
            record.update({"status": report["status"], "result": report.get("result", ""),
                           "error": report.get("error"), "usage": report.get("usage"),
                           "finished_at": report["finished_at"]})
            self._write(sid, record)
        elif record["status"] == "RUNNING" and not self._worker_alive(record):
            if time.time() - record.get("started_at", 0) > 5:
                record["status"] = "STALLED"
                record["error"] = "Worker not running and no completion receipt"
                self._write(sid, record)
        return {key: record.get(key) for key in (
            "status", "result", "error", "usage", "started_at", "finished_at"
        )}

    def events(self, sid, *, after=0):
        self._read(sid)  # validate ownership of a persisted native session
        if type(after) is not int or after < 0:
            raise ValueError("invalid event offset")
        path = self._path(sid).with_suffix(".events.jsonl")
        events, last = [], after
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # concurrent append may still be in progress
                    seq = event.get("seq", 0)
                    if seq > after:
                        events.append(event)
                        last = seq
                        if len(events) == 100:
                            break
        return events

    def decide(self, sid, request_id, decision):
        if decision not in {"allow", "deny"} or not isinstance(request_id, str) or len(request_id) > 128:
            raise ValueError("invalid Claude permission decision")
        if self.status(sid)["status"] != "RUNNING":
            raise RuntimeError("Claude session is not running")
        events = self._path(sid).with_suffix(".events.jsonl")
        pending = {}
        if events.exists():
            with events.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("kind") == "permission_request":
                        pending[event.get("request_id")] = event.get("input", {})
                    elif event.get("kind") == "permission_cancelled":
                        pending.pop(event.get("request_id"), None)
        if request_id not in pending:
            raise ValueError("no matching pending permission request")
        decisions = self._path(sid).with_suffix(".decisions.jsonl")
        if decisions.exists():
            with decisions.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        if json.loads(line).get("request_id") == request_id:
                            raise ValueError("permission request already answered")
                    except json.JSONDecodeError:
                        continue
        with decisions.open("a", encoding="utf-8") as f:
            os.chmod(decisions, 0o600)
            f.write(json.dumps({"request_id": request_id, "decision": decision,
                                "input": pending[request_id]}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return {"request_id": request_id, "decision": decision, "accepted": True}

    def send(self, sid, message):
        if self.status(sid)["status"] == "RUNNING":
            raise RuntimeError("Claude session is busy")
        if self.status(sid)["status"] == "CANCELLED":
            raise RuntimeError("Claude session cancelled")
        if not isinstance(message, str) or not message.strip() or len(message) > 40000:
            raise ValueError("invalid message")
        self._spawn(sid, message, True)

    def resume(self, sid):
        return self.status(sid)

    def cancel(self, sid):
        record = self._read(sid)
        if record["status"] == "RUNNING" and self._worker_alive(record):
            os.killpg(record["pid"], signal.SIGTERM)
        record["status"] = "CANCELLED"
        self._write(sid, record)


def make():
    return ClaudeCodeAdapter()

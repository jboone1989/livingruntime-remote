"""Native Codex CLI Adapter for the existing LivingRuntime Remote Agent contract.

Codex owns model execution, Skills, tools, MCP, and native thread history.
Remote owns policy, durable job ownership, events, and cancellation.
"""
from __future__ import annotations
import os
import re
import shutil
from pathlib import Path

from agent_policy import claude_workspace
from livingruntime_adapter_claude import ClaudeCodeAdapter
from agent_adapters import AgentCapabilities

_MODELS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}$")
_ALLOWED = {"model", "sandbox", "timeout_seconds", "profile"}
_SANDBOXES = {"read-only", "workspace-write"}


def validate_options(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _ALLOWED:
        raise ValueError("unsupported Codex CLI option")
    result = dict(value)
    if "sandbox" in result and result["sandbox"] not in _SANDBOXES:
        raise ValueError("Codex full-access sandbox is not supported over Remote")
    for name in ("model", "profile"):
        if name in result and (not isinstance(result[name], str) or not _MODELS.fullmatch(result[name])):
            raise ValueError("invalid Codex " + name)
    if "timeout_seconds" in result and (
        type(result["timeout_seconds"]) is not int or
        not 30 <= result["timeout_seconds"] <= 21600
    ):
        raise ValueError("invalid Codex timeout_seconds")
    return result


class CodexAdapter(ClaudeCodeAdapter):
    adapter_id = "codex"

    def __init__(self, executable=None):
        # No side effects on import/registration. A missing CLI fails when
        # start() is invoked rather than preventing Claude from registering.
        candidate = executable or os.environ.get("LIVINGRUNTIME_CODEX_EXECUTABLE") or shutil.which("codex")
        self.executable = str(Path(candidate).resolve()) if candidate else None
        self.root = Path(os.environ.get(
            "LIVINGRUNTIME_CODEX_JOBS", str(Path.home() / ".livingruntime" / "codex-jobs")
        ))
        self.worker = Path(__file__).with_name("codex_worker.py")

    def start(self, goal, *, project, device, options=None):
        if self.executable is None or not Path(self.executable).is_file():
            raise RuntimeError("Official Codex CLI executable not installed or not configured")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 40000:
            raise ValueError("invalid goal")
        validated = validate_options(options)
        # Reuse the existing explicit device:project workspace map; the
        # adapter-specific launch allowlist remains separately enforced.
        cwd = claude_workspace(device, project)
        import uuid
        sid = str(uuid.uuid4())
        self._write(sid, {"session_id": sid, "cwd": cwd,
                          "executable": self.executable, "status": "PENDING",
                          "project": project, "device": device, "options": validated})
        return self._spawn(sid, goal, False)

    def _worker_alive(self, record):
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            return False
        try:
            cmd = (Path("/proc") / str(pid) / "cmdline").read_bytes()
            return b"codex_worker.py" in cmd and str(self._path(record["session_id"])).encode() in cmd
        except OSError:
            return False


def make():
    return CodexAdapter()

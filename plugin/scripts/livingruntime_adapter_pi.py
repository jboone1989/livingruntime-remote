"""Native Pi coding agent adapter: optional model-backed sub-agent.

Pi owns model/tool loops and its session. Remote owns placement, launch
authorization, durable job receipts, and event visibility.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from agent_adapters import AgentCapabilities
from agent_policy import load as load_policy
from livingruntime_adapter_claude import ClaudeCodeAdapter

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}$")
_OPTIONS = {"model", "provider", "timeout_seconds", "tools", "env_file"}


def validate_options(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - _OPTIONS:
        raise ValueError("unsupported Pi option")
    result = dict(value)
    for key in ("model", "provider"):
        if key in result and (not isinstance(result[key], str)
                              or not _IDENTIFIER.fullmatch(result[key])):
            raise ValueError("invalid Pi " + key)
    if "timeout_seconds" in result and (type(result["timeout_seconds"]) is not int
                                         or not 30 <= result["timeout_seconds"] <= 21600):
        raise ValueError("invalid Pi timeout_seconds")
    if "tools" in result and (
        not isinstance(result["tools"], list)
        or not result["tools"]
        or not set(result["tools"]) <= {"read", "grep", "find", "ls", "edit", "write", "bash"}
    ):
        raise ValueError("invalid Pi tools")
    if "env_file" in result and (not isinstance(result["env_file"], str)
                                  or not result["env_file"] or len(result["env_file"]) > 256):
        raise ValueError("invalid Pi env_file")
    return result


def pi_workspace(device, project):
    from agent_policy import claude_workspace
    # Reuse the existing explicit project mapping, with a Pi-specific override.
    policy = load_policy()
    workspaces = policy.get("pi_workspaces", {})
    if not isinstance(workspaces, dict):
        raise ValueError("invalid Pi workspace policy")
    path = workspaces.get(f"{device}:{project}")
    if path is None:
        return claude_workspace(device, project)
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise PermissionError("Pi workspace is not available")
    return str(resolved)


class PiAdapter(ClaudeCodeAdapter):
    adapter_id = "pi"

    def __init__(self, executable=None):
        trusted = Path.home() / ".livingruntime" / "pi-harness" / "node_modules" / ".bin" / "pi"
        candidate = (executable or os.environ.get("LIVINGRUNTIME_PI_EXECUTABLE")
                     or (str(trusted) if trusted.is_file() else None)
                     or shutil.which("pi") or str(Path(__file__).resolve().parents[1]
                                                  / "node_modules" / ".bin" / "pi"))
        self.executable = str(Path(candidate).resolve()) if candidate else None
        self.root = Path(os.environ.get(
            "LIVINGRUNTIME_PI_JOBS", str(Path.home() / ".livingruntime" / "pi-jobs")
        ))
        self.worker = Path(__file__).with_name("pi_worker.py")

    def capabilities(self):
        return AgentCapabilities(resume=True, messaging=True, events=True, cancel=True)

    def start(self, goal, *, project, device, options=None):
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 40000:
            raise ValueError("invalid Pi goal")
        options = validate_options(options)
        provider = options.get("provider", "bigmodel-official")
        model = options.get("model", "glm-5.3")
        cwd = pi_workspace(device, project)
        default_env = Path.home() / ".livingruntime" / "official-glm.env"
        owner_env_selected = (provider == "bigmodel-official" and
                              "env_file" not in options and default_env.is_file())
        if owner_env_selected:
            options["env_file"] = str(default_env)
        executable = self.executable
        if not executable or not Path(executable).is_file():
            trusted = Path.home() / ".livingruntime" / "pi-harness" / "node_modules" / ".bin" / "pi"
            if not trusted.is_file():
                raise RuntimeError("Native Pi CLI is not installed in trusted Connector home")
            executable = str(trusted.resolve())
        if Path(executable).resolve().is_relative_to(Path(cwd).resolve()):
            raise PermissionError("Pi executable must be outside the agent-writable workspace")
        if "env_file" in options:
            workspace = Path(cwd).resolve(strict=True)
            target = (workspace / options["env_file"]).resolve(strict=True)
            owner_home_env = (owner_env_selected and
                              target == default_env.resolve(strict=True) and
                              not target.is_relative_to(workspace))
            if not (target.is_relative_to(workspace) or owner_home_env) or not target.is_file():
                raise PermissionError("Pi env_file must be explicit in the workspace or the trusted owner-home default")
            options["env_file"] = str(target)
        sid = str(uuid.uuid4())
        self._write(sid, {
            "session_id": sid, "cwd": cwd, "executable": executable,
            "status": "PENDING", "project": project, "device": device,
            "options": {**options, "provider": provider, "model": model},
        })
        return self._spawn(sid, goal, False)

    def _worker_alive(self, record):
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            return False
        try:
            cmd = (Path("/proc") / str(pid) / "cmdline").read_bytes()
            native = b"agent-worker\x00pi\x00" in cmd
            return (b"pi_worker.py" in cmd or native) and str(
                self._path(record["session_id"])).encode() in cmd
        except OSError:
            return False


def make():
    return PiAdapter()

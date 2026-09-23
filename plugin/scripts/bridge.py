from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from contract import (
    DEFAULT_HTTP_PORT,
    HANDSHAKE_TOOLS,
    LOOPBACK_HOSTS,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    REMOTE_IDENTITY,
    REMOTE_TOOLS,
    SECRET_KEYS,
    schema_hash,
)
from configmodel import (
    all_units,
    host_for,
    list_projects as catalog_projects,
    normalize,
    resolve_path,
    resolve_unit,
)

NAME = PLUGIN_NAME
IDENTITY = REMOTE_IDENTITY
VERSION = PLUGIN_VERSION
MAX_OUTPUT_BYTES = 262144
DEFAULT_TIMEOUT = 30

server = FastMCP(NAME)
_LAST_SUCCESS_UNIX = 0.0
_PROCESS_STARTED = time.time()
_LAST_TOOLS_LIST_UNIX = 0.0
_ACTIVE_TRANSPORT = "stdio"
_HTTP_BIND = ""

_ALLOWED_EXECUTABLES = {
    "cargo", "df", "du", "find", "go", "head", "make", "node",
    "npm", "npx", "pnpm", "ps", "pytest", "python", "python3",
    "ruff", "sed", "ss", "tail", "uv",
}
_BLOCKED_INLINE = {
    "python": {"-c"},
    "python3": {"-c"},
    "node": {"-e", "--eval"},
}
_BLOCKED_GIT = {"credential", "daemon", "shell"}

_REMOTE_AGENT = r"""
import hashlib, json, os, pathlib, signal, subprocess, sys
req = json.load(sys.stdin)
roots = [pathlib.Path(x).expanduser().resolve(strict=True) for x in req["roots"]]
def inside(value, exists=True):
    p = pathlib.Path(value).expanduser()
    if exists or p.exists() or p.is_symlink():
        p = p.resolve(strict=True)
    else:
        p = p.parent.resolve(strict=True) / p.name
    for root in roots:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            pass
    raise PermissionError("path outside configured roots: " + str(p))
def digest(p):
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
op = req["op"]
if op == "exec":
    cwd = inside(req["cwd"])
    if not cwd.is_dir():
        raise ValueError("cwd must be a directory")
    p = subprocess.run(
        req["argv"], cwd=str(cwd), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=req["timeout"], check=False,
    )
    limit = req["max_output"]
    print(json.dumps({
        "returncode": p.returncode,
        "stdout": p.stdout[:limit].decode("utf-8", errors="replace"),
        "stderr": p.stderr[:limit].decode("utf-8", errors="replace"),
        "cwd": str(cwd),
    }))
elif op == "read_file":
    p = inside(req["path"])
    if not p.is_file():
        raise ValueError("path is not a file")
    with p.open("rb") as fh:
        fh.seek(req["offset"])
        data = fh.read(req["max_bytes"])
    print(json.dumps({
        "path": str(p), "offset": req["offset"], "bytes_read": len(data),
        "content": data.decode("utf-8", errors="replace"), "sha256": digest(p),
    }))
elif op == "write_file":
    p = inside(req["path"], exists=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    expected = req.get("expected_sha256")
    if expected is not None:
        if not p.exists():
            raise FileNotFoundError("expected_sha256 supplied but file does not exist")
        if digest(p) != expected:
            raise RuntimeError("sha256 mismatch")
    with p.open("a" if req["mode"] == "append" else "w", encoding="utf-8") as fh:
        fh.write(req["content"])
    print(json.dumps({"path": str(p), "mode": req["mode"], "bytes": p.stat().st_size, "sha256": digest(p)}))
elif op == "list_dir":
    p = inside(req["path"])
    if not p.is_dir():
        raise ValueError("path is not a directory")
    children = sorted(p.iterdir(), key=lambda x: x.name)
    rows = []
    for child in children[:req["max_entries"]]:
        rows.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file" if child.is_file() else "other",
            "size": child.stat().st_size if child.is_file() else None,
        })
    print(json.dumps({"path": str(p), "entries": rows, "truncated": len(children) > len(rows)}))
elif op == "process":
    uid = os.getuid()
    if req["action"] == "list":
        rows = []
        for proc in sorted(pathlib.Path("/proc").iterdir(), key=lambda p: p.name):
            if not proc.name.isdigit():
                continue
            try:
                if proc.stat().st_uid != uid:
                    continue
                cwd = (proc / "cwd").resolve(strict=True)
                inside(str(cwd))
                command = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
                if req.get("contains") and req["contains"] not in command:
                    continue
                rows.append({"pid": int(proc.name), "cwd": str(cwd), "command": command})
                if len(rows) >= 500:
                    break
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                pass
        print(json.dumps({"processes": rows}))
    elif req["action"] == "terminate":
        pid = int(req.get("pid") or 0)
        if pid <= 1:
            raise ValueError("refusing pid <= 1")
        proc = pathlib.Path("/proc") / str(pid)
        if proc.stat().st_uid != uid:
            raise PermissionError("process owner mismatch")
        cwd = (proc / "cwd").resolve(strict=True)
        inside(str(cwd))
        os.kill(pid, signal.SIGTERM)
        print(json.dumps({"pid": pid, "signal": "SIGTERM", "cwd": str(cwd)}))
    else:
        raise ValueError("action must be list or terminate")
else:
    raise ValueError("unknown operation")
"""


def _config_path() -> str:
    return os.environ.get(
        "LIVINGRUNTIME_REMOTE_CONFIG",
        os.path.join(os.path.expanduser("~"), ".livingruntime", "remote.json"),
    )


def _config() -> dict[str, Any]:
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            value = json.load(fh)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"LivingRuntime Remote is not configured; run scripts/configure.py first ({_config_path()})"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("remote config must be a JSON object")
    return normalize(value)


def _host(project: str | None = None) -> str:
    return host_for(_config(), project=project)["ssh_host"]


def _roots(project: str | None = None) -> tuple[str, ...]:
    return tuple(host_for(_config(), project=project)["roots"])


def _units(project: str | None = None) -> set[str]:
    return all_units(_config(), project=project)


def _audit(tool: str, ok: bool, detail: dict[str, Any]) -> None:
    directory = os.path.join(os.path.expanduser("~"), ".livingruntime")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "remote-audit.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"ts": time.time(), "tool": tool, "ok": ok, "detail": detail},
            ensure_ascii=False, sort_keys=True,
        ) + "\n")


def _ssh(
    remote_argv: list[str],
    *,
    stdin: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    project: str | None = None,
) -> dict[str, Any]:
    command = shlex.join(remote_argv)
    started = time.monotonic()
    proc = subprocess.run(
        [
            "ssh", "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
            _host(project), command,
        ],
        input=b"" if stdin is None else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=min(150, max(1, int(timeout)) + 12),
        check=False,
    )
    def bounded(data: bytes) -> str:
        if len(data) > MAX_OUTPUT_BYTES:
            data = data[:MAX_OUTPUT_BYTES] + b"\n...[truncated]..."
        return data.decode("utf-8", errors="replace")
    return {
        "returncode": proc.returncode,
        "stdout": bounded(proc.stdout),
        "stderr": bounded(proc.stderr),
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }


def _remote(op: str, payload: dict[str, Any], timeout: int = DEFAULT_TIMEOUT, project: str | None = None) -> dict[str, Any]:
    request = {"op": op, "roots": list(_roots(project)), **payload}
    result = _ssh(
        ["python3", "-c", _REMOTE_AGENT],
        stdin=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
        project=project,
    )
    if result["returncode"] != 0:
        raise RuntimeError(result["stderr"] or result["stdout"] or "remote operation failed")
    return json.loads(result["stdout"])


def _validate_exec(argv: list[str]) -> None:
    if not argv:
        raise ValueError("argv is required")
    executable = os.path.basename(str(argv[0]))
    if executable not in _ALLOWED_EXECUTABLES:
        raise PermissionError(f"executable is not allowlisted: {executable}")
    if any(arg in _BLOCKED_INLINE.get(executable, set()) for arg in argv[1:]):
        raise PermissionError(f"inline code flag is blocked for {executable}")
    if any("\x00" in str(arg) for arg in argv):
        raise ValueError("NUL bytes are not allowed")


def _validate_git(args: list[str]) -> None:
    if not args:
        raise ValueError("git args are required")
    subcommand = next((x for x in args if not str(x).startswith("-")), "")
    if not subcommand:
        raise ValueError("git subcommand is required")
    if subcommand in _BLOCKED_GIT:
        raise PermissionError(f"git subcommand is blocked: {subcommand}")
    blocked_flags = {"-c", "-C", "--git-dir", "--work-tree", "--config-env"}
    if any(
        arg in blocked_flags
        or str(arg).startswith("--git-dir=")
        or str(arg).startswith("--work-tree=")
        for arg in args
    ):
        raise PermissionError("git path/config override flags are blocked")
    joined = " ".join(map(str, args))
    if "credential.helper" in joined or "core.sshCommand" in joined:
        raise PermissionError("credential and ssh command overrides are blocked")


_HUNK_RE = re.compile(r"^@@(?:\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?)?\s*(?:@@)?\s*$")


def apply_unified_diff(original: str, patch: str) -> str:
    """Apply a single-file unified diff. Raises on mismatch; never returns a partial file."""
    text = (patch or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("patch is empty")
    hunks = _parse_unified_hunks(text)
    if not hunks:
        raise ValueError("patch contains no hunks")
    result = original.split("\n")
    for hunk in reversed(hunks):
        result = _apply_hunk(result, hunk)
    return "\n".join(result)


def _parse_unified_hunks(patch: str) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    lines = patch.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.startswith("---")
            or line.startswith("+++")
            or line.startswith("diff ")
            or line.startswith("index ")
            or line.startswith("old mode")
            or line.startswith("new mode")
        ):
            index += 1
            continue
        match = _HUNK_RE.match(line)
        if match is None:
            if line.startswith("@@"):
                raise ValueError(f"invalid hunk header: {line}")
            index += 1
            continue
        old_start = int(match.group(1)) if match.group(1) else None
        index += 1
        old_block: list[str] = []
        new_block: list[str] = []
        while index < len(lines):
            body = lines[index]
            if body.startswith("@@") or body.startswith("diff ") or body.startswith("--- "):
                break
            if body.startswith("\\"):
                index += 1
                continue
            if body.startswith("+"):
                new_block.append(body[1:])
            elif body.startswith("-"):
                old_block.append(body[1:])
            elif body.startswith(" "):
                old_block.append(body[1:])
                new_block.append(body[1:])
            elif body == "":
                if index == len(lines) - 1:
                    index += 1
                    break
                old_block.append("")
                new_block.append("")
            else:
                raise ValueError(f"invalid patch line: {body[:80]}")
            index += 1
        hunks.append({"old_start": old_start, "old": old_block, "new": new_block})
    return hunks


def _apply_hunk(lines: list[str], hunk: dict[str, Any]) -> list[str]:
    old = list(hunk["old"])
    new = list(hunk["new"])
    start = hunk["old_start"]
    if start is not None and old:
        idx = start - 1
        if idx >= 0 and lines[idx:idx + len(old)] == old:
            return lines[:idx] + new + lines[idx + len(old):]
    if not old:
        if start is None:
            raise ValueError("insertion hunk requires a location")
        idx = max(0, start - 1)
        return lines[:idx] + new + lines[idx:]
    matches = [i for i in range(0, len(lines) - len(old) + 1) if lines[i:i + len(old)] == old]
    if len(matches) != 1:
        raise ValueError("patch hunk does not match the current file uniquely")
    idx = matches[0]
    return lines[:idx] + new + lines[idx + len(old):]


def _capability_snapshot() -> dict[str, Any]:
    advertised = advertised_tool_names()
    advertised_set = set(advertised)
    expected = list(REMOTE_TOOLS)
    missing = [name for name in expected if name not in advertised_set]
    extra = [name for name in advertised if name not in expected]
    available = [name for name in expected if name in advertised_set]
    handshake = [name for name in HANDSHAKE_TOOLS if name in advertised_set]
    healthy = not missing and not extra
    return {
        "identity": IDENTITY,
        "version": VERSION,
        "schema_hash": schema_hash(),
        "tools": handshake,
        "server_tools": available,
        "available": available,
        "missing": missing,
        "extra": extra,
        "healthy": healthy,
        "health_scope": "mcp_tools_list",
    }


def _sanitized_error(text: str) -> str:
    line = (text or "").strip().splitlines()[-1] if text and text.strip() else ""
    return line[:300]


def _contains_secret(payload: Any) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_KEYS):
                return True
            if _contains_secret(value):
                return True
        return False
    if isinstance(payload, list):
        return any(_contains_secret(item) for item in payload)
    return False


def _transport_endpoint() -> dict[str, Any]:
    if _ACTIVE_TRANSPORT == "streamable-http":
        return {
            "reachable": True,
            "transport": "streamable-http",
            "bind": _HTTP_BIND,
            "mcp_path": "/mcp",
            "public_ingress": False,
        }
    return {
        "reachable": True,
        "transport": "stdio",
        "bind": "stdio",
        "mcp_path": None,
        "public_ingress": False,
    }


@server.tool(
    name="capabilities",
    annotations=ToolAnnotations(
        title="Remote capabilities",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def capabilities() -> dict[str, Any]:
    """Report Remote identity, schema hash, handshake tools, and full registry drift.

    `tools` is the handshake subset. `server_tools` / `available` are MCP `tools/list`.
    `healthy` and `health_scope=mcp_tools_list` do not describe ChatGPT Custom App action cache.
    """
    result = _capability_snapshot()
    _audit("capabilities", bool(result["healthy"]), {
        "identity": result["identity"],
        "version": result["version"],
        "schema_hash": result["schema_hash"],
        "available": result["available"],
        "missing": result["missing"],
    })
    return result


@server.tool(
    name="connection_status",
    annotations=ToolAnnotations(
        title="Connection status",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def connection_status() -> dict[str, Any]:
    """Bootstrap reachability even when a ChatGPT session has not attached the full toolset.

    This process cannot observe ChatGPT UI binding. If ChatGPT never calls this tool,
    the session is unattached; that state cannot be reported from here.
    remote_reachable follows the SSH probe. toolset_loaded is true only when this
    process's tools/list matches REMOTE_TOOLS. reason is null when the registry is
    healthy; otherwise a server-side drift reason. ChatGPT session attachment is
    not faked as chat_session_not_attached.
    """
    global _LAST_SUCCESS_UNIX
    host_alias = _host()
    result = _ssh(
        ["python3", "-c", "import getpass,json,os,socket; print(json.dumps({'user':getpass.getuser(),'hostname':socket.gethostname(),'cwd':os.getcwd()}))"],
        timeout=12,
    )
    ok = result["returncode"] == 0
    remote = None
    if ok:
        remote = json.loads(result["stdout"].strip())
        _LAST_SUCCESS_UNIX = time.time()
    cap = _capability_snapshot()
    toolset_loaded = bool(cap["healthy"])
    payload = {
        "ok": ok,
        "remote_reachable": ok,
        "toolset_loaded": toolset_loaded,
        "reason": None if toolset_loaded else "server_tool_registry_drift",
        "endpoint": _transport_endpoint(),
        "gateway": {
            "reachable": ok,
            "kind": "bounded-ssh",
            "name": NAME,
            "identity": IDENTITY,
            "version": VERSION,
        },
        "remote_host": {
            "alias": host_alias,
            "user": None if remote is None else remote.get("user"),
            "hostname": None if remote is None else remote.get("hostname") or remote.get("host"),
            "cwd": None if remote is None else remote.get("cwd"),
        },
        "connectivity": {
            "ssh": ok,
            "gateway": ok,
            "authenticated": ok,
            "authorized": ok,
            "latency_ms": result["duration_ms"],
            "last_success_unix": _LAST_SUCCESS_UNIX or None,
        },
        "configured_roots": list(_roots()),
        "configured_systemd_units": sorted(_units()),
        "projects": catalog_projects(_config()),
        "core_tools": list(REMOTE_TOOLS),
        "capabilities": cap,
        "error": None if ok else _sanitized_error(result["stderr"] or result["stdout"] or "SSH connection failed"),
    }
    if _contains_secret(payload):
        raise RuntimeError("connection_status refused to return secret-bearing fields")
    _audit("connection_status", ok, {"host_alias": host_alias, "latency_ms": result["duration_ms"]})
    return payload


@server.tool(
    name="list_projects",
    annotations=ToolAnnotations(
        title="List configured projects",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_projects() -> dict[str, Any]:
    """List named projects from ~/.livingruntime/remote.json so tools can use project= instead of absolute paths."""
    cfg = _config()
    rows = catalog_projects(cfg)
    result = {
        "default_host": cfg["default_host"],
        "hosts": [
            {
                "id": host["id"],
                "ssh_host": host["ssh_host"],
                "roots": list(host["roots"]),
                "units": list(host["units"]),
            }
            for host in cfg["hosts"].values()
        ],
        "projects": rows,
    }
    _audit("list_projects", True, {"count": len(rows)})
    return result


@server.tool(
    name="exec",
    annotations=ToolAnnotations(
        title="Execute development command",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def exec(
    argv: list[str],
    cwd: str | None = None,
    project: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one allowlisted development executable; shell command strings are not accepted."""
    _validate_exec(argv)
    timeout = min(120, max(1, int(timeout_seconds)))
    workdir = resolve_path(_config(), cwd, project=project) if (cwd or project) else _roots(project)[0]
    result = _remote("exec", {
        "argv": argv, "cwd": workdir,
        "timeout": timeout, "max_output": MAX_OUTPUT_BYTES,
    }, timeout=timeout + 2, project=project)
    _audit("exec", True, {"argv": argv, "cwd": result.get("cwd"), "project": project, "returncode": result.get("returncode")})
    return result


def _openai_continuation_path(session_id: str) -> Path:
    session_id = str(session_id).strip()
    if not session_id or len(session_id) > 256:
        raise ValueError("session_id must be a bounded non-empty string")
    root = Path.home() / ".livingruntime" / "openai-continuations"
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"
    return root / name


def _load_openai_continuation(session_id: str) -> dict[str, Any] | None:
    path = _openai_continuation_path(session_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("invalid OpenAI continuation state")
    return value


def _save_openai_continuation(session_id: str, value: dict[str, Any]) -> None:
    path = _openai_continuation_path(session_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _clear_openai_continuation(session_id: str) -> None:
    try:
        _openai_continuation_path(session_id).unlink()
    except FileNotFoundError:
        pass


def _pi_job_command(
    command: str,
    job_id: str,
    pi_remote_dir: str,
    job_root: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    if command not in {"job-status", "job-wait"}:
        raise ValueError("unsupported Pi job command")
    job_id = str(job_id).strip()
    pi_remote_dir = str(pi_remote_dir).strip()
    if not job_id or len(job_id) > 128:
        raise ValueError("job_id must be a bounded non-empty string")
    if not pi_remote_dir or len(pi_remote_dir) > 4096:
        raise ValueError("pi_remote_dir must be a bounded non-empty path")
    payload: dict[str, Any] = {"jobId": job_id}
    if job_root:
        if len(job_root) > 4096:
            raise ValueError("job_root path is too long")
        payload["jobRoot"] = job_root
    bounded_timeout = min(90, max(1, int(timeout_seconds)))
    if command == "job-wait":
        payload["timeoutMs"] = bounded_timeout * 1000
    result = exec(
        argv=[
            "node",
            pi_remote_dir.rstrip("/") + "/cli.js",
            command,
            json.dumps(payload, separators=(",", ":")),
        ],
        cwd=pi_remote_dir,
        timeout_seconds=min(120, bounded_timeout + 10),
    )
    if int(result.get("returncode", 1)) != 0:
        raise RuntimeError(str(result.get("stderr") or "Pi Remote job command failed"))
    stdout = result.get("stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        raise RuntimeError("Pi Remote job command returned no JSON")
    parsed = json.loads(stdout)
    if not isinstance(parsed, dict):
        raise RuntimeError("Pi Remote job command returned non-object JSON")
    return parsed


@server.tool(
    name="watch_pi_job",
    annotations=ToolAnnotations(
        title="Watch Pi Remote job",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def watch_pi_job(
    job_id: str,
    pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
    job_root: str | None = None,
) -> dict[str, Any]:
    """Inspect an existing Pi Remote detached job before arming OpenAI continuation."""
    state = _pi_job_command("job-status", job_id, pi_remote_dir, job_root, timeout_seconds=15)
    return {
        "jobId": job_id,
        "piRemoteDir": pi_remote_dir,
        "jobRoot": job_root,
        "state": state,
        "terminal": state.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"},
    }


@server.tool(
    name="wait_pi_job_completion",
    annotations=ToolAnnotations(
        title="Wait for Pi Remote job completion",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def wait_pi_job_completion(
    job_id: str,
    pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
    job_root: str | None = None,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Event-driven bounded wait for an existing Pi Remote detached job."""
    return _pi_job_command(
        "job-wait", job_id, pi_remote_dir, job_root, timeout_seconds=timeout_seconds
    )


@server.tool(
    name="bind_openai_pi_continuation",
    annotations=ToolAnnotations(
        title="Bind Pi job to OpenAI session",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def bind_openai_pi_continuation(
    session_id: str,
    job_id: str,
    pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
    job_root: str | None = None,
) -> dict[str, Any]:
    """Bind a watched Pi job to a Codex/Work session for the plugin Stop hook."""
    session_id = str(session_id).strip()
    job_id = str(job_id).strip()
    pi_remote_dir = str(pi_remote_dir).strip()
    if not job_id or len(job_id) > 128:
        raise ValueError("job_id must be a bounded non-empty string")
    if not pi_remote_dir or len(pi_remote_dir) > 4096:
        raise ValueError("pi_remote_dir must be a bounded non-empty path")
    if job_root is not None and len(job_root) > 4096:
        raise ValueError("job_root path is too long")
    _save_openai_continuation(session_id, {
        "session_id": session_id,
        "job_id": job_id,
        "pi_remote_dir": pi_remote_dir,
        "job_root": job_root,
        "updated_at": time.time(),
    })
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "Pi Remote continuation is armed for this OpenAI session.",
        }
    }


@server.tool(
    name="continue_openai_pi_job",
    annotations=ToolAnnotations(
        title="Continue OpenAI session after Pi job",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def continue_openai_pi_job(
    session_id: str,
    timeout_seconds: int = 540,
) -> dict[str, Any]:
    """Codex/Work Stop-hook helper that waits for Pi and requests a new continuation turn."""
    binding = _load_openai_continuation(session_id)
    if binding is None:
        return {"continue": True}

    deadline = time.monotonic() + min(540, max(1, int(timeout_seconds)))
    latest: dict[str, Any] | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        latest = _pi_job_command(
            "job-wait",
            str(binding["job_id"]),
            str(binding["pi_remote_dir"]),
            binding.get("job_root"),
            timeout_seconds=min(90, max(1, int(remaining))),
        )
        state = latest.get("state") if isinstance(latest.get("state"), dict) else {}
        terminal = bool(latest.get("terminal")) or state.get("status") in {
            "SUCCEEDED", "FAILED", "CANCELLED"
        }
        if terminal:
            _clear_openai_continuation(session_id)
            status = str(state.get("status") or "terminal")
            return {
                "decision": "block",
                "reason": (
                    f"Pi Remote job {binding['job_id']} completed with status {status}. "
                    "Continue this same development task now: inspect the durable Pi job/session "
                    "result, review changes and tests, then proceed to the next required step "
                    "without asking the user to say continue."
                ),
            }
        if not latest.get("timedOut"):
            break

    return {
        "decision": "block",
        "reason": (
            f"Pi Remote job {binding['job_id']} is still running after the bounded wait. "
            "Do not poll it from the model. End this continuation so the OpenAI Stop hook "
            "can resume waiting event-driven on the next stop."
        ),
    }


@server.tool(
    name="read_file",
    annotations=ToolAnnotations(
        title="Read remote file",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def read_file(
    path: str,
    project: str | None = None,
    offset: int = 0,
    max_bytes: int = 131072,
) -> dict[str, Any]:
    """Read a bounded byte range from a file. Prefer project= plus a project-relative path."""
    target = resolve_path(_config(), path, project=project)
    result = _remote("read_file", {
        "path": target, "offset": max(0, int(offset)),
        "max_bytes": min(MAX_OUTPUT_BYTES, max(1, int(max_bytes))),
    }, project=project)
    _audit("read_file", True, {"path": result["path"], "project": project, "bytes_read": result["bytes_read"]})
    return result


@server.tool(
    name="write_file",
    annotations=ToolAnnotations(
        title="Write remote file",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def write_file(
    path: str,
    content: str,
    project: str | None = None,
    mode: str = "replace",
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Write UTF-8 text under a configured root or named project, optionally guarded by current SHA-256."""
    if mode not in {"replace", "append"}:
        raise ValueError("mode must be replace or append")
    if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("content exceeds maximum write size")
    target = resolve_path(_config(), path, project=project)
    result = _remote("write_file", {
        "path": target, "content": content, "mode": mode,
        "expected_sha256": expected_sha256,
    }, project=project)
    _audit("write_file", True, {"path": result["path"], "project": project, "mode": mode, "bytes": result["bytes"]})
    return result


@server.tool(
    name="apply_patch",
    annotations=ToolAnnotations(
        title="Apply unified diff to remote file",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def apply_patch(
    path: str,
    patch: str,
    expected_sha256: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Apply a unified diff under a configured root. Failure leaves the file unchanged."""
    target = resolve_path(_config(), path, project=project)
    current = _remote("read_file", {
        "path": target, "offset": 0, "max_bytes": MAX_OUTPUT_BYTES,
    }, project=project)
    old_sha = current["sha256"]
    if int(current.get("bytes_read") or 0) >= MAX_OUTPUT_BYTES:
        raise ValueError("file is too large for apply_patch")
    if expected_sha256 is not None and old_sha != expected_sha256:
        raise RuntimeError("sha256 mismatch")
    try:
        updated = apply_unified_diff(current["content"], patch)
    except Exception:
        _audit("apply_patch", False, {
            "path": current["path"],
            "old_sha256": old_sha,
            "new_sha256": None,
            "timestamp": time.time(),
        })
        raise
    if updated == current["content"]:
        _audit("apply_patch", True, {
            "path": current["path"],
            "old_sha256": old_sha,
            "new_sha256": old_sha,
            "timestamp": time.time(),
        })
        return {"path": current["path"], "changed": False, "sha256": old_sha}
    written = _remote("write_file", {
        "path": target,
        "content": updated,
        "mode": "replace",
        "expected_sha256": old_sha,
    }, project=project)
    _audit("apply_patch", True, {
        "path": written["path"],
        "old_sha256": old_sha,
        "new_sha256": written["sha256"],
        "timestamp": time.time(),
    })
    return {"path": written["path"], "changed": True, "sha256": written["sha256"]}


@server.tool(
    name="list_dir",
    annotations=ToolAnnotations(
        title="List remote directory",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_dir(path: str | None = None, project: str | None = None, max_entries: int = 200) -> dict[str, Any]:
    """List one remote directory under a configured root or named project."""
    target = resolve_path(_config(), path, project=project)
    result = _remote("list_dir", {
        "path": target, "max_entries": min(1000, max(1, int(max_entries))),
    }, project=project)
    _audit("list_dir", True, {"path": result["path"], "project": project, "entries": len(result["entries"])})
    return result


@server.tool(
    name="git",
    annotations=ToolAnnotations(
        title="Run remote Git operation",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def git(
    args: list[str],
    repo_path: str | None = None,
    project: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run a Git subcommand. Prefer project=virtualbrain instead of an absolute repo_path."""
    _validate_git(args)
    timeout = min(120, max(1, int(timeout_seconds)))
    cwd = resolve_path(_config(), repo_path, project=project)
    result = _remote("exec", {
        "argv": ["git", *args], "cwd": cwd,
        "timeout": timeout, "max_output": MAX_OUTPUT_BYTES,
    }, timeout=timeout + 2, project=project)
    _audit("git", True, {"repo_path": cwd, "project": project, "args": args, "returncode": result.get("returncode")})
    return result


@server.tool(
    name="process",
    annotations=ToolAnnotations(
        title="Inspect or terminate remote process",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def process(action: str, pid: int | None = None, contains: str | None = None) -> dict[str, Any]:
    """List scoped same-user processes or SIGTERM one process whose cwd is under a configured root."""
    result = _remote("process", {"action": action, "pid": pid, "contains": contains})
    _audit("process", True, {"action": action, "pid": pid})
    return result


@server.tool(
    name="systemd",
    annotations=ToolAnnotations(
        title="Control allowlisted remote service",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def systemd(action: str, unit: str | None = None, project: str | None = None) -> dict[str, Any]:
    """Inspect or control an explicitly allowlisted remote systemd service."""
    selected = resolve_unit(_config(), unit, project=project)
    if action not in {"status", "is-active", "start", "stop", "restart"}:
        raise ValueError("unsupported systemd action")
    argv = ["systemctl", action, selected]
    if action in {"start", "stop", "restart"}:
        argv = ["sudo", "-n", *argv]
    result = _ssh(argv, timeout=30, project=project)
    _audit("systemd", result["returncode"] == 0, {"action": action, "unit": selected, "project": project})
    return result


@server.tool(
    name="logs",
    annotations=ToolAnnotations(
        title="Read allowlisted remote service logs",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def logs(
    unit: str | None = None,
    project: str | None = None,
    lines: int = 200,
    since_minutes: int = 60,
) -> dict[str, Any]:
    """Read bounded journal logs. Prefer project=ferro instead of guessing a unit name."""
    selected = resolve_unit(_config(), unit, project=project)
    result = _ssh([
        "journalctl", "--no-pager", "-u", selected,
        "-n", str(min(1000, max(1, int(lines)))),
        "--since", f"{min(10080, max(1, int(since_minutes)))} minutes ago",
    ], timeout=30, project=project)
    _audit("logs", result["returncode"] == 0, {"unit": selected, "project": project, "lines": lines})
    return result


@server.tool(
    name="diagnostics",
    annotations=ToolAnnotations(
        title="Remote session diagnostics",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def diagnostics() -> dict[str, Any]:
    """Report Remote identity, schema hash, and whether this process has the full tool registry.

    SESSION_ATTACH here means this MCP process registered REMOTE_TOOLS. It cannot see
    whether a ChatGPT composer chip is bound.
    """
    cap = _capability_snapshot()
    last_refresh = _LAST_TOOLS_LIST_UNIX or _PROCESS_STARTED
    result = {
        "remote_id": IDENTITY,
        "version": VERSION,
        "session_attach": bool(cap["healthy"]),
        "last_tool_refresh": last_refresh,
        "schema_hash": cap["schema_hash"],
        "process_started": _PROCESS_STARTED,
        "available": cap["available"],
        "missing": cap["missing"],
    }
    _audit("diagnostics", True, {
        "remote_id": IDENTITY,
        "version": VERSION,
        "session_attach": result["session_attach"],
        "schema_hash": result["schema_hash"],
        "last_tool_refresh": last_refresh,
    })
    return result


def advertised_tool_names() -> list[str]:
    global _LAST_TOOLS_LIST_UNIX
    tools = server._tool_manager.list_tools()
    _LAST_TOOLS_LIST_UNIX = time.time()
    return sorted(tool.name for tool in tools)


def configure_http_transport(host: str, port: int) -> None:
    global _ACTIVE_TRANSPORT, _HTTP_BIND
    if host not in LOOPBACK_HOSTS:
        raise ValueError("HTTP transport must bind loopback only; do not expose the MCP port to the public internet")
    settings = getattr(server, "settings", None)
    if settings is None:
        raise RuntimeError("FastMCP settings are unavailable")
    settings.host = host
    settings.port = int(port)
    if hasattr(settings, "stateless_http"):
        settings.stateless_http = True
    if hasattr(settings, "json_response"):
        settings.json_response = True
    _ACTIVE_TRANSPORT = "streamable-http"
    _HTTP_BIND = f"{host}:{int(port)}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LivingRuntime Remote MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("LIVINGRUNTIME_REMOTE_TRANSPORT", "stdio"),
        help="stdio for Codex/local plugins; http for Secure MCP Tunnel on loopback",
    )
    parser.add_argument("--host", default=os.environ.get("LIVINGRUNTIME_REMOTE_HTTP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LIVINGRUNTIME_REMOTE_HTTP_PORT", str(DEFAULT_HTTP_PORT))),
    )
    args = parser.parse_args(argv)
    if args.transport == "http":
        configure_http_transport(args.host, args.port)
        server.run(transport="streamable-http")
        return
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

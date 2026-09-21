from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from policy import (
    CommandPolicyError,
    resolve_allowed_path,
    validate_exec_argv,
    validate_git_args,
    validate_systemd_action,
)

NAME = "LivingRuntime Remote MCP Gateway"
VERSION = "0.2.0"
CORE_TOOLS = ("connection_status", "read_file", "git", "logs")
MAX_OUTPUT_BYTES = int(os.environ.get("MCP_GATEWAY_MAX_OUTPUT_BYTES", "262144"))
DEFAULT_TIMEOUT = min(120, max(1, int(os.environ.get("MCP_GATEWAY_DEFAULT_TIMEOUT", "30"))))
TOKEN = os.environ.get("MCP_GATEWAY_TOKEN", "")
BIND_HOST = os.environ.get("MCP_GATEWAY_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("MCP_GATEWAY_PORT", "8765"))
AUDIT_LOG = Path(
    os.environ.get(
        "MCP_GATEWAY_AUDIT_LOG",
        str(Path.home() / ".local/state/remote-mcp-gateway/audit.jsonl"),
    )
)
_LAST_SUCCESS_UNIX = 0.0

mcp = MCPServer(NAME)


def _roots() -> tuple[Path, ...]:
    raw = os.environ.get("MCP_GATEWAY_ALLOWED_ROOTS", str(Path.home()))
    roots = tuple(Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip())
    if not roots:
        raise RuntimeError("MCP_GATEWAY_ALLOWED_ROOTS must contain at least one path")
    return roots


def _systemd_units() -> set[str]:
    raw = os.environ.get("MCP_GATEWAY_SYSTEMD_UNITS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _audit(tool: str, ok: bool, detail: dict[str, Any]) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "tool": tool, "ok": ok, "detail": detail}
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _bounded_text(data: bytes) -> str:
    if len(data) > MAX_OUTPUT_BYTES:
        data = data[:MAX_OUTPUT_BYTES] + b"\n...[truncated]..."
    return data.decode("utf-8", errors="replace")


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    timeout = min(120, max(1, int(timeout_seconds or DEFAULT_TIMEOUT)))
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env={**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
    )
    return {
        "argv": argv,
        "cwd": str(cwd) if cwd else None,
        "returncode": proc.returncode,
        "stdout": _bounded_text(proc.stdout),
        "stderr": _bounded_text(proc.stderr),
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _host_identity() -> dict[str, Any]:
    return {
        "user": getpass.getuser(),
        "hostname": socket.gethostname(),
        "cwd": os.getcwd(),
    }


def _connection_payload(*, authenticated: bool) -> dict[str, Any]:
    global _LAST_SUCCESS_UNIX
    started = time.monotonic()
    roots = [str(p) for p in _roots()]
    roots_ok = all(Path(p).exists() for p in roots)
    latency_ms = round((time.monotonic() - started) * 1000, 2)
    ok = roots_ok and authenticated
    if ok:
        _LAST_SUCCESS_UNIX = time.time()
    return {
        "ok": ok,
        "endpoint": {
            "reachable": True,
            "transport": "streamable-http",
            "bind": f"{BIND_HOST}:{BIND_PORT}",
            "mcp_path": "/mcp",
            "public_ingress": False,
        },
        "gateway": {
            "reachable": True,
            "kind": "remote-mcp-gateway",
            "name": NAME,
            "version": VERSION,
        },
        "remote_host": _host_identity(),
        "connectivity": {
            "ssh": None,
            "gateway": True,
            "authenticated": authenticated,
            "authorized": authenticated and roots_ok,
            "auth_configured": bool(TOKEN),
            "latency_ms": latency_ms,
            "last_success_unix": _LAST_SUCCESS_UNIX or None,
        },
        "configured_roots": roots,
        "configured_systemd_units": sorted(_systemd_units()),
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "default_timeout_seconds": DEFAULT_TIMEOUT,
        "core_tools": list(CORE_TOOLS),
    }


@mcp.tool(
    name="connection_status",
    title="Connection status",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, destructive_hint=False),
)
def connection_status() -> dict[str, Any]:
    """Report whether this private MCP gateway is reachable, authenticated, and authorized."""
    result = _connection_payload(authenticated=True)
    _audit("connection_status", bool(result["ok"]), {"latency_ms": result["connectivity"]["latency_ms"]})
    return result


@mcp.tool(
    name="gateway_status",
    title="Gateway status",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, destructive_hint=False),
)
def gateway_status() -> dict[str, Any]:
    """Compatibility alias for connection_status; prefer connection_status."""
    status = _connection_payload(authenticated=True)
    result = {
        "name": NAME,
        "version": VERSION,
        "allowed_roots": status["configured_roots"],
        "systemd_units": status["configured_systemd_units"],
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "default_timeout_seconds": DEFAULT_TIMEOUT,
        "auth_configured": bool(TOKEN),
        "preferred_tool": "connection_status",
        "ok": status["ok"],
    }
    _audit("gateway_status", True, {})
    return result


@mcp.tool(
    title="Execute development command",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def exec(argv: list[str], cwd: str | None = None, timeout_seconds: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run one allowlisted executable without a shell."""
    try:
        validate_exec_argv(argv)
        resolved_cwd = resolve_allowed_path(cwd or str(_roots()[0]), _roots(), must_exist=True)
        if not resolved_cwd.is_dir():
            raise CommandPolicyError("cwd must be a directory")
        result = _run(argv, cwd=resolved_cwd, timeout_seconds=timeout_seconds)
        _audit("exec", True, {"argv": argv, "cwd": str(resolved_cwd), "returncode": result["returncode"]})
        return result
    except Exception as exc:
        _audit("exec", False, {"argv": argv, "cwd": cwd, "error": str(exc)})
        raise


@mcp.tool(
    title="Read file",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, destructive_hint=False),
)
def read_file(path: str, offset: int = 0, max_bytes: int = 131072) -> dict[str, Any]:
    """Read a bounded byte range from a file under the allowed roots."""
    try:
        resolved = resolve_allowed_path(path, _roots(), must_exist=True)
        if not resolved.is_file():
            raise ValueError("path is not a file")
        max_bytes = min(MAX_OUTPUT_BYTES, max(1, int(max_bytes)))
        offset = max(0, int(offset))
        with resolved.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(max_bytes)
        result = {
            "path": str(resolved),
            "offset": offset,
            "bytes_read": len(data),
            "content": data.decode("utf-8", errors="replace"),
            "sha256": _sha256(resolved),
        }
        _audit("read_file", True, {"path": str(resolved), "offset": offset, "bytes_read": len(data)})
        return result
    except Exception as exc:
        _audit("read_file", False, {"path": path, "error": str(exc)})
        raise


@mcp.tool(
    title="Write file",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def write_file(
    path: str,
    content: str,
    mode: str = "replace",
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Write UTF-8 text under an allowed root, optionally with compare-and-swap protection."""
    try:
        resolved = resolve_allowed_path(path, _roots(), must_exist=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if expected_sha256 is not None:
            if not resolved.exists():
                raise FileNotFoundError("expected_sha256 supplied but file does not exist")
            actual = _sha256(resolved)
            if not hmac.compare_digest(actual, expected_sha256):
                raise RuntimeError(f"sha256 mismatch: expected {expected_sha256}, actual {actual}")
        if mode not in {"replace", "append"}:
            raise ValueError("mode must be replace or append")
        if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("content exceeds MCP_GATEWAY_MAX_OUTPUT_BYTES")
        open_mode = "a" if mode == "append" else "w"
        with resolved.open(open_mode, encoding="utf-8") as fh:
            fh.write(content)
        result = {"path": str(resolved), "mode": mode, "sha256": _sha256(resolved), "bytes": resolved.stat().st_size}
        _audit("write_file", True, {"path": str(resolved), "mode": mode, "bytes": result["bytes"]})
        return result
    except Exception as exc:
        _audit("write_file", False, {"path": path, "mode": mode, "error": str(exc)})
        raise


@mcp.tool(
    title="List directory",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, destructive_hint=False),
)
def list_dir(path: str, max_entries: int = 200) -> dict[str, Any]:
    """List one directory under the allowed roots."""
    try:
        resolved = resolve_allowed_path(path, _roots(), must_exist=True)
        if not resolved.is_dir():
            raise ValueError("path is not a directory")
        max_entries = min(1000, max(1, int(max_entries)))
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda p: p.name)[:max_entries]:
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file" if child.is_file() else "other",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        result = {"path": str(resolved), "entries": entries, "truncated": len(entries) >= max_entries}
        _audit("list_dir", True, {"path": str(resolved), "entries": len(entries)})
        return result
    except Exception as exc:
        _audit("list_dir", False, {"path": path, "error": str(exc)})
        raise


@mcp.tool(
    title="Run Git operation",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def git(repo_path: str, args: list[str], timeout_seconds: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run a bounded git subcommand inside an allowed repository path."""
    try:
        repo = resolve_allowed_path(repo_path, _roots(), must_exist=True)
        if not repo.is_dir():
            raise ValueError("repo_path must be a directory")
        validate_git_args(args)
        result = _run(["git", *args], cwd=repo, timeout_seconds=timeout_seconds)
        _audit("git", True, {"repo": str(repo), "args": args, "returncode": result["returncode"]})
        return result
    except Exception as exc:
        _audit("git", False, {"repo": repo_path, "args": args, "error": str(exc)})
        raise


@mcp.tool(
    title="Inspect or terminate development process",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def process(action: str, pid: int | None = None, contains: str | None = None) -> dict[str, Any]:
    """List processes or terminate a same-user process whose cwd is inside an allowed root."""
    try:
        if action == "list":
            rows = []
            for proc_dir in sorted(Path("/proc").iterdir(), key=lambda p: p.name):
                if not proc_dir.name.isdigit():
                    continue
                try:
                    if proc_dir.stat().st_uid != os.getuid():
                        continue
                    cwd = (proc_dir / "cwd").resolve(strict=True)
                    resolve_allowed_path(str(cwd), _roots(), must_exist=True)
                    raw = (proc_dir / "cmdline").read_bytes().replace(b"\x00", b" ").strip()
                    command = raw.decode("utf-8", errors="replace")
                    if contains and contains not in command:
                        continue
                    rows.append({"pid": int(proc_dir.name), "cwd": str(cwd), "command": command})
                    if len(rows) >= 500:
                        break
                except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                    continue
            out = {"processes": rows}
            _audit("process", True, {"action": action, "contains": contains, "count": len(rows)})
            return out
        if action != "terminate" or pid is None:
            raise ValueError("action must be list or terminate with pid")
        pid = int(pid)
        if pid <= 1:
            raise ValueError("refusing to terminate pid <= 1")
        proc_dir = Path(f"/proc/{pid}")
        owner = proc_dir.stat().st_uid
        if owner != os.getuid():
            raise PermissionError("process owner does not match gateway user")
        cwd = (proc_dir / "cwd").resolve()
        resolve_allowed_path(str(cwd), _roots(), must_exist=True)
        os.kill(pid, signal.SIGTERM)
        _audit("process", True, {"action": action, "pid": pid, "cwd": str(cwd)})
        return {"pid": pid, "signal": "SIGTERM", "cwd": str(cwd)}
    except Exception as exc:
        _audit("process", False, {"action": action, "pid": pid, "error": str(exc)})
        raise


@mcp.tool(
    title="Control allowlisted service",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def systemd(action: str, unit: str) -> dict[str, Any]:
    """Inspect or control an explicitly allowlisted systemd unit."""
    try:
        allowed = _systemd_units()
        validate_systemd_action(action, unit, allowed)
        argv = ["sudo", "-n", "systemctl", action, unit] if action in {"start", "stop", "restart"} else ["systemctl", action, unit]
        result = _run(argv, timeout_seconds=30)
        _audit("systemd", True, {"action": action, "unit": unit, "returncode": result["returncode"]})
        return result
    except Exception as exc:
        _audit("systemd", False, {"action": action, "unit": unit, "error": str(exc)})
        raise


@mcp.tool(
    title="Read allowlisted service logs",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, destructive_hint=False),
)
def logs(unit: str, lines: int = 200, since_minutes: int = 60) -> dict[str, Any]:
    """Read bounded journal logs for an explicitly allowlisted systemd unit."""
    try:
        if unit not in _systemd_units():
            raise PermissionError("unit is not allowlisted")
        lines = min(1000, max(1, int(lines)))
        since_minutes = min(10080, max(1, int(since_minutes)))
        result = _run(
            ["journalctl", "--no-pager", "-u", unit, "-n", str(lines), "--since", f"{since_minutes} minutes ago"],
            timeout_seconds=30,
        )
        _audit("logs", True, {"unit": unit, "lines": lines, "returncode": result["returncode"]})
        return result
    except Exception as exc:
        _audit("logs", False, {"unit": unit, "error": str(exc)})
        raise


class BearerAuth:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            if scope.get("path") == "/healthz":
                body = b'{"ok":true}'
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
            if not TOKEN:
                await self._deny(send, 503, "gateway token is not configured")
                return
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            supplied = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
            expected = f"Bearer {TOKEN}"
            if not hmac.compare_digest(supplied, expected):
                await self._deny(send, 401, "unauthorized")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send: Any, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


_mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True)
app = BearerAuth(_mcp_app)

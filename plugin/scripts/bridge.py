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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import localexec
from agent_adapters import registry as agent_registry, load_installed
import agent_sessions
import agent_policy
import filesystem_delete
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
from credentials import (
    create_lease as create_credential_lease,
    list_handles as credential_snapshot,
    list_leases as credential_lease_snapshot,
    resolve_secret_for_lease,
    revoke_lease as revoke_credential_lease_runtime,
)
from execution_status import (
    record_tool_event as record_execution_tool_event,
    snapshot as execution_status_snapshot,
    tracked_cognition_request_id,
)
from jobs import (
    acknowledge_completion as acknowledge_runtime_completion,
    attach_backend as attach_runtime_backend,
    claim_completion as claim_runtime_completion,
    checkpoint as checkpoint_runtime_job,
    create as create_runtime_job,
    ensure_backend_job,
    find_by_backend,
    get as get_runtime_job,
    list_jobs as runtime_jobs_snapshot,
    mark_completion_delivered as mark_runtime_completion_delivered,
    sync_backend_status,
    update_runtime as update_runtime_job,
)
from cognition import (
    claim_request as claim_cognition_request,
    claim_next as claim_next_cognition_request,
    complete as complete_cognition_request,
    dispatch_lane as cognition_dispatch_lane,
    get as get_cognition_request,
    get_status as get_cognition_request_status,
    resumable_watcher_id as cognition_watcher_id,
    submit as submit_cognition_request,
    wait_pending as wait_pending_cognition_request,
)
from permissions import (
    approve as approve_dynamic_exec,
    classify as classify_dynamic_exec,
    deny as deny_dynamic_exec,
    ensure_request as ensure_dynamic_exec_request,
    is_granted as dynamic_exec_grant,
    revoke as revoke_dynamic_exec,
    snapshot as exec_permission_snapshot,
)

NAME = PLUGIN_NAME
IDENTITY = REMOTE_IDENTITY
VERSION = PLUGIN_VERSION
MAX_OUTPUT_BYTES = 262144
DEFAULT_TIMEOUT = 30
DEFERRED_RESTART_DELAY_SECONDS = 3.0
DEFERRED_SELF_RESTART_UNITS = frozenset({
    "livingruntime-tunnel.service",
    "livingruntime-remote-relay.service",
})

server = FastMCP(NAME)
_AGENT_ADAPTER_LOAD_ERROR: str | None = None
try:
    load_installed()
except (OSError, ValueError, ImportError, AttributeError, TypeError, RuntimeError) as exc:
    # An optional adapter must never disable direct Remote mode.
    _AGENT_ADAPTER_LOAD_ERROR = type(exc).__name__
_LAST_SUCCESS_UNIX = 0.0
_DEVICE_LAST_SUCCESS_UNIX: dict[str, float] = {}
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

_DEFERRED_RESTART_WORKER = r"""
import json, os, pathlib, subprocess, sys, time

receipt_path = pathlib.Path(sys.argv[1])
unit = sys.argv[2]
receipt_stat = receipt_path.stat()
receipt_uid = receipt_stat.st_uid
receipt_gid = receipt_stat.st_gid

def save(payload):
    temp = receipt_path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
        os.chown(temp, receipt_uid, receipt_gid)
    except OSError:
        pass
    temp.replace(receipt_path)

payload = json.loads(receipt_path.read_text(encoding="utf-8"))
payload["status"] = "RESTARTING"
payload["started_at"] = time.time()
save(payload)

try:
    completed = subprocess.run(
        ["/usr/bin/systemctl", "restart", unit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        check=False,
    )
    payload["returncode"] = completed.returncode
    payload["status"] = "RESTART_COMPLETED" if completed.returncode == 0 else "RESTART_FAILED"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        payload["error"] = detail[-500:]
except Exception as exc:
    payload["returncode"] = None
    payload["status"] = "RESTART_FAILED"
    payload["error"] = str(exc)[-500:]

payload["finished_at"] = time.time()
save(payload)
raise SystemExit(0 if payload["status"] == "RESTART_COMPLETED" else 1)
"""

_DEFERRED_RESTART_SCHEDULER = r"""
import json, pathlib, re, subprocess, sys, time

req = json.load(sys.stdin)
receipt_id = str(req["receipt_id"])
unit = str(req["unit"])
delay_seconds = float(req["delay_seconds"])
root = pathlib.Path(req["root"]).expanduser().resolve(strict=True)
worker_code = str(req["worker_code"])

if not re.fullmatch(r"restart-[0-9a-f]{20}", receipt_id):
    raise ValueError("invalid restart receipt id")
if not unit.endswith(".service") or "/" in unit or "\\" in unit:
    raise ValueError("invalid systemd service name")
if delay_seconds < 1.0 or delay_seconds > 30.0:
    raise ValueError("invalid restart delay")

directory = root / ".livingruntime" / "restart-receipts"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
receipt_path = directory / (receipt_id + ".json")

def save(payload):
    temp = receipt_path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(receipt_path)

now = time.time()
payload = {
    "receipt_id": receipt_id,
    "action": "restart",
    "unit": unit,
    "status": "RESTART_SCHEDULING",
    "scheduled_at": now,
    "not_before": now + delay_seconds,
    "receipt_path": str(receipt_path),
}
save(payload)

transient_name = "livingruntime-" + receipt_id
argv = [
    "sudo", "-n", "systemd-run",
    "--quiet",
    "--collect",
    "--unit=" + transient_name,
    "--on-active=" + str(delay_seconds) + "s",
    "/usr/bin/python3", "-c", worker_code, str(receipt_path), unit,
]
completed = subprocess.run(
    argv,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=15,
    check=False,
)
if completed.returncode != 0:
    payload["status"] = "RESTART_SCHEDULE_FAILED"
    payload["returncode"] = completed.returncode
    detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
    payload["error"] = detail[-500:]
    save(payload)
    print(json.dumps(payload))
    raise SystemExit(completed.returncode)

payload["status"] = "RESTART_SCHEDULED"
payload["returncode"] = 0
save(payload)
print(json.dumps(payload))
"""

_DETACHED_EXEC_WORKER = r"""
import json, os, pathlib, selectors, signal, subprocess, sys, time

receipt_path = pathlib.Path(sys.argv[1])
spec_path = pathlib.Path(sys.argv[2])
receipt_stat = receipt_path.stat()
receipt_uid = receipt_stat.st_uid
receipt_gid = receipt_stat.st_gid

def save(payload):
    temp = receipt_path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
        os.chown(temp, receipt_uid, receipt_gid)
    except OSError:
        pass
    temp.replace(receipt_path)

def tail_append(buffer, chunk, limit):
    buffer.extend(chunk)
    if len(buffer) > limit:
        del buffer[:-limit]

payload = json.loads(receipt_path.read_text(encoding="utf-8"))
spec = json.loads(spec_path.read_text(encoding="utf-8"))
heartbeat_seconds = max(1.0, min(float(spec.get("heartbeat_seconds") or 10.0), 60.0))
stall_seconds = max(heartbeat_seconds * 2.0, min(float(spec.get("stall_seconds") or 300.0), 86400.0))
tail_limit = max(1024, min(int(spec.get("tail_bytes") or 32768), 131072))
cancelled = False
cancel_started_at = None
child = None

def request_cancel(_signum, _frame):
    global cancelled, cancel_started_at
    cancelled = True
    if cancel_started_at is None:
        cancel_started_at = time.time()
    if child is not None and child.poll() is None:
        try:
            child.terminate()
        except ProcessLookupError:
            pass

signal.signal(signal.SIGTERM, request_cancel)
signal.signal(signal.SIGINT, request_cancel)

now = time.time()
payload.update({
    "status": "STARTING",
    "terminal": False,
    "worker_pid": os.getpid(),
    "heartbeat_seconds": heartbeat_seconds,
    "stall_seconds": stall_seconds,
    "last_heartbeat_at": now,
    "last_progress_at": now,
})
save(payload)

stdout_tail = bytearray()
stderr_tail = bytearray()
selector = selectors.DefaultSelector()
try:
    child = subprocess.Popen(
        list(spec["argv"]),
        cwd=str(spec["cwd"]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    if child.stdout is None or child.stderr is None:
        raise RuntimeError("detached child pipes unavailable")
    selector.register(child.stdout, selectors.EVENT_READ, "stdout")
    selector.register(child.stderr, selectors.EVENT_READ, "stderr")
    now = time.time()
    payload.update({
        "status": "RUNNING",
        "terminal": False,
        "child_pid": child.pid,
        "started_at": now,
        "last_heartbeat_at": now,
        "last_progress_at": now,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
    })
    save(payload)
    last_save = now

    while True:
        progressed = False
        for key, _mask in selector.select(timeout=min(1.0, heartbeat_seconds)):
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                chunk = b""
            if chunk:
                progressed = True
                if key.data == "stdout":
                    payload["stdout_bytes"] = int(payload.get("stdout_bytes") or 0) + len(chunk)
                    tail_append(stdout_tail, chunk, tail_limit)
                else:
                    payload["stderr_bytes"] = int(payload.get("stderr_bytes") or 0) + len(chunk)
                    tail_append(stderr_tail, chunk, tail_limit)
            else:
                try:
                    selector.unregister(key.fileobj)
                except Exception:
                    pass

        now = time.time()
        if progressed:
            payload["last_progress_at"] = now
        if cancelled and child.poll() is None and cancel_started_at is not None:
            if now - cancel_started_at >= 5.0:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass

        running = child.poll() is None
        if now - last_save >= heartbeat_seconds or progressed or not running:
            progress_age = max(0.0, now - float(payload.get("last_progress_at") or now))
            payload.update({
                "last_heartbeat_at": now,
                "progress_age_seconds": round(progress_age, 3),
                "stdout_tail": stdout_tail.decode("utf-8", errors="replace"),
                "stderr_tail": stderr_tail.decode("utf-8", errors="replace"),
            })
            if running:
                payload["status"] = "QUIET" if progress_age >= stall_seconds else "RUNNING"
                payload["terminal"] = False
            save(payload)
            last_save = now

        if not running and not selector.get_map():
            break

    returncode = child.wait()
    now = time.time()
    payload.update({
        "returncode": returncode,
        "finished_at": now,
        "last_heartbeat_at": now,
        "progress_age_seconds": max(
            0.0, now - float(payload.get("last_progress_at") or now)
        ),
        "stdout_tail": stdout_tail.decode("utf-8", errors="replace"),
        "stderr_tail": stderr_tail.decode("utf-8", errors="replace"),
        "terminal": True,
        "status": "CANCELLED" if cancelled else ("SUCCEEDED" if returncode == 0 else "FAILED"),
    })
    save(payload)
except Exception as exc:
    now = time.time()
    payload.update({
        "status": "CANCELLED" if cancelled else "FAILED",
        "terminal": True,
        "finished_at": now,
        "last_heartbeat_at": now,
        "returncode": None if child is None else child.poll(),
        "error": (type(exc).__name__ + ": " + str(exc))[-1000:],
        "stdout_tail": stdout_tail.decode("utf-8", errors="replace"),
        "stderr_tail": stderr_tail.decode("utf-8", errors="replace"),
    })
    save(payload)
finally:
    try:
        spec_path.unlink()
    except OSError:
        pass
"""

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
def detached_receipt_path(execution_id):
    value = str(execution_id or "")
    if not value.startswith("dex_") or not value[4:].isalnum() or len(value) > 64:
        raise ValueError("invalid detached execution id")
    directory = roots[0] / ".livingruntime" / "detached-exec"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / (value + ".json")
def save_detached_receipt(payload):
    target = detached_receipt_path(payload["execution_id"])
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(target)
op = req["op"]
if op == "exec":
    cwd = inside(req["cwd"])
    if not cwd.is_dir():
        raise ValueError("cwd must be a directory")
    if req.get("detached"):
        execution_id = str(req.get("execution_id") or "")
        receipt_path = detached_receipt_path(execution_id)
        spec_path = receipt_path.with_suffix(".spec.json")
        heartbeat_seconds = max(1.0, min(float(req.get("heartbeat_seconds") or 10.0), 60.0))
        stall_seconds = max(
            heartbeat_seconds * 2.0,
            min(float(req.get("stall_seconds") or 300.0), 86400.0),
        )
        spec = {
            "argv": list(req["argv"]),
            "cwd": str(cwd),
            "heartbeat_seconds": heartbeat_seconds,
            "stall_seconds": stall_seconds,
            "tail_bytes": min(int(req.get("tail_bytes") or 32768), 131072),
        }
        spec_path.write_text(json.dumps(spec, sort_keys=True) + "\n", encoding="utf-8")
        try:
            spec_path.chmod(0o600)
        except OSError:
            pass
        now = __import__("time").time()
        result = {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "stdout_tail": "",
            "stderr_tail": "",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "cwd": str(cwd),
            "detached": True,
            "terminal": False,
            "status": "STARTING",
            "execution_id": execution_id,
            "created_at": now,
            "last_heartbeat_at": now,
            "last_progress_at": now,
            "heartbeat_seconds": heartbeat_seconds,
            "stall_seconds": stall_seconds,
        }
        save_detached_receipt(result)
        p = subprocess.Popen(
            [sys.executable, "-c", str(req["worker_code"]), str(receipt_path), str(spec_path)],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        result["worker_pid"] = p.pid
        result["pid"] = p.pid
        save_detached_receipt(result)
        print(json.dumps(result))
        raise SystemExit(0)
    limit = req["max_output"]
    try:
        p = subprocess.run(
            req["argv"], cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=req["timeout"], check=False,
        )
        print(json.dumps({
            "returncode": p.returncode,
            "stdout": p.stdout[:limit].decode("utf-8", errors="replace"),
            "stderr": p.stderr[:limit].decode("utf-8", errors="replace"),
            "cwd": str(cwd),
            "status": "COMPLETED",
            "timed_out": False,
        }))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", errors="replace")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8", errors="replace")
        print(json.dumps({
            "returncode": None,
            "stdout": stdout[:limit].decode("utf-8", errors="replace"),
            "stderr": stderr[:limit].decode("utf-8", errors="replace"),
            "cwd": str(cwd),
            "status": "TIMED_OUT",
            "timed_out": True,
            "timeout_seconds": req["timeout"],
        }))
elif op == "exec_receipt":
    execution_id = str(req.get("execution_id") or "")
    target = detached_receipt_path(execution_id)
    if not target.is_file():
        print(json.dumps({"found": False, "execution_id": execution_id}))
    else:
        payload = json.loads(target.read_text(encoding="utf-8"))
        now = __import__("time").time()
        heartbeat_at = float(payload.get("last_heartbeat_at") or payload.get("created_at") or now)
        progress_at = float(payload.get("last_progress_at") or payload.get("created_at") or now)
        payload["heartbeat_age_seconds"] = round(max(0.0, now - heartbeat_at), 3)
        payload["progress_age_seconds"] = round(max(0.0, now - progress_at), 3)
        observed = str(payload.get("status") or "UNKNOWN")
        worker_pid = int(payload.get("worker_pid") or payload.get("pid") or 0)
        child_pid = int(payload.get("child_pid") or 0)
        def alive(pid):
            if pid <= 1:
                return False
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                return False
        worker_alive = alive(worker_pid)
        child_alive = alive(child_pid)
        payload["worker_alive"] = worker_alive
        payload["child_alive"] = child_alive
        if not payload.get("terminal"):
            heartbeat_limit = max(
                30.0, float(payload.get("heartbeat_seconds") or 10.0) * 3.0
            )
            if not worker_alive and child_alive:
                observed = "ORPHANED"
            elif not worker_alive:
                observed = "LOST"
            elif payload["heartbeat_age_seconds"] >= heartbeat_limit:
                observed = "HEARTBEAT_STALE"
        payload["observed_status"] = observed
        payload["found"] = True
        print(json.dumps(payload))
elif op == "exec_cancel":
    execution_id = str(req.get("execution_id") or "")
    target = detached_receipt_path(execution_id)
    if not target.is_file():
        print(json.dumps({"found": False, "execution_id": execution_id}))
    else:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if payload.get("terminal"):
            payload["found"] = True
            payload["cancel_requested"] = False
            print(json.dumps(payload))
        else:
            worker_pid = int(payload.get("worker_pid") or payload.get("pid") or 0)
            if worker_pid <= 1:
                raise RuntimeError("detached worker pid is unavailable")
            os.killpg(worker_pid, signal.SIGTERM)
            print(json.dumps({
                "found": True,
                "execution_id": execution_id,
                "worker_pid": worker_pid,
                "cancel_requested": True,
                "status": payload.get("status"),
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
elif op == "delete_path":
    namespace = {}
    exec(compile(req["delete_worker_source"], "<bounded-filesystem-delete>", "exec"), namespace)
    result = namespace["delete_path"](
        req["path"], req["roots"], recursive=req["recursive"], dry_run=req["dry_run"],
        expected_sha256=req.get("expected_sha256"),
        protected_paths=req.get("protected_paths", []),
    )
    print(json.dumps(result))
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

_HOST_INVENTORY_AGENT = r"""
import json, os, pathlib, shutil, socket, subprocess, sys

req = json.load(sys.stdin)

def read_meminfo():
    values = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            parts = raw.strip().split()
            if not parts:
                continue
            value = int(parts[0])
            if len(parts) > 1 and parts[1].lower() == "kb":
                value *= 1024
            values[key] = value
    except (OSError, ValueError):
        pass
    return values

def read_uptime():
    try:
        return float(pathlib.Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None

memory = read_meminfo()
try:
    load = list(os.getloadavg())
except (AttributeError, OSError):
    load = []

disks = []
for raw in req.get("roots") or []:
    path = pathlib.Path(raw)
    try:
        usage = shutil.disk_usage(path)
        disks.append({
            "path": str(path),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        })
    except OSError as exc:
        disks.append({"path": str(path), "error": str(exc)[:200]})

projects = []
for item in req.get("projects") or []:
    path = pathlib.Path(item["path"])
    projects.append({
        "name": item["name"],
        "path": item["path"],
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "git_repo": (path / ".git").exists(),
        "units": list(item.get("units") or []),
    })

services = []
for unit in req.get("units") or []:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        state = proc.stdout.decode("utf-8", errors="replace").strip()
        services.append({
            "unit": unit,
            "state": state or "unknown",
            "active": proc.returncode == 0 and state == "active",
        })
    except (OSError, subprocess.TimeoutExpired):
        services.append({"unit": unit, "state": "unknown", "active": False})

print(json.dumps({
    "hostname": socket.gethostname(),
    "cpu_count": os.cpu_count(),
    "load_average": load,
    "uptime_seconds": read_uptime(),
    "memory": {
        "total_bytes": memory.get("MemTotal"),
        "available_bytes": memory.get("MemAvailable"),
    },
    "disks": disks,
    "projects": projects,
    "services": services,
}))
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


def _host(project: str | None = None, device: str | None = None) -> str:
    return host_for(_config(), project=project, host_id=device)["ssh_host"]


def _transport(project: str | None = None, device: str | None = None) -> str:
    return host_for(_config(), project=project, host_id=device).get("transport", "ssh")


def _roots(project: str | None = None, device: str | None = None) -> tuple[str, ...]:
    return tuple(host_for(_config(), project=project, host_id=device)["roots"])


def _units(project: str | None = None, device: str | None = None) -> set[str]:
    return all_units(_config(), project=project, host_id=device)


def _audit(tool: str, ok: bool, detail: dict[str, Any]) -> None:
    observed_at = time.time()
    directory = os.path.join(os.path.expanduser("~"), ".livingruntime")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "remote-audit.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"ts": observed_at, "tool": tool, "ok": ok, "detail": detail},
            ensure_ascii=False, sort_keys=True,
        ) + "\n")
    try:
        record_execution_tool_event(
            tool,
            ok,
            detail,
            observed_at=observed_at,
        )
    except Exception:
        # Execution telemetry must never make a real Remote tool fail.
        pass


def _ssh(
    remote_argv: list[str],
    *,
    stdin: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    project: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    if _transport(project, device) != "ssh":
        raise RuntimeError("SSH operation requested for a native local device")
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
            _host(project, device), command,
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


def _remote(
    op: str,
    payload: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
    project: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    request = {"op": op, "roots": list(_roots(project, device)), **payload}
    if _transport(project, device) == "local":
        return localexec.handle(request)
    result = _ssh(
        ["python3", "-c", _REMOTE_AGENT],
        stdin=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
        project=project,
        device=device,
    )
    if result["returncode"] != 0:
        raise RuntimeError(result["stderr"] or result["stdout"] or "remote operation failed")
    return json.loads(result["stdout"])


def _detached_execution_id(
    *,
    host_id: str,
    project: str | None,
    cwd: str,
    argv: list[str],
) -> str:
    seed = json.dumps(
        {
            "host_id": host_id,
            "project": project,
            "cwd": cwd,
            "argv": [str(x) for x in argv],
            "pid": os.getpid(),
            "time_ns": time.time_ns(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "dex_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _validate_exec(argv: list[str]) -> None:
    if not argv:
        raise ValueError("argv is required")
    executable = os.path.basename(str(argv[0]))
    if any(arg in _BLOCKED_INLINE.get(executable, set()) for arg in argv[1:]):
        raise PermissionError(f"inline code flag is blocked for {executable}")
    if any("\x00" in str(arg) for arg in argv):
        raise ValueError("NUL bytes are not allowed")
    if executable not in _ALLOWED_EXECUTABLES and classify_dynamic_exec(argv) == "hard_deny":
        raise PermissionError(f"executable is hard-denied: {executable}")


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


def _inventory_for_device(cfg: dict[str, Any], device_id: str) -> dict[str, Any]:
    host = host_for(cfg, host_id=device_id)
    projects = [
        {
            "name": project["name"],
            "path": project["path"],
            "units": list(project["units"]),
        }
        for project in catalog_projects(cfg)
        if project["host"] == device_id
    ]
    request = {
        "roots": list(host["roots"]),
        "projects": projects,
        "units": sorted(all_units(cfg, host_id=device_id)),
    }
    if host.get("transport") == "local":
        try:
            payload = localexec.inventory(request)
        except Exception as exc:
            return {"ok": False, "error": _sanitized_error(str(exc))}
    else:
        result = _ssh(
            ["python3", "-c", _HOST_INVENTORY_AGENT],
            stdin=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            timeout=20,
            device=device_id,
        )
        if result["returncode"] != 0:
            return {
                "ok": False,
                "error": _sanitized_error(result["stderr"] or result["stdout"] or "inventory probe failed"),
            }
        try:
            payload = json.loads(result["stdout"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"ok": False, "error": "inventory probe returned invalid JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "inventory probe returned invalid payload"}
    if _contains_secret(payload):
        return {"ok": False, "error": "inventory probe returned disallowed fields"}
    return {"ok": True, **payload}


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
def connection_status(device: str | None = None) -> dict[str, Any]:
    """Bootstrap reachability even when a ChatGPT session has not attached the full toolset.

    This process cannot observe ChatGPT UI binding. If ChatGPT never calls this tool,
    the session is unattached; that state cannot be reported from here.
    remote_reachable follows the SSH probe. toolset_loaded is true only when this
    process's tools/list matches REMOTE_TOOLS. reason is null when the registry is
    healthy; otherwise a server-side drift reason. ChatGPT session attachment is
    not faked as chat_session_not_attached.
    """
    global _LAST_SUCCESS_UNIX
    cfg = _config()
    selected_host = host_for(cfg, host_id=device)
    device_id = selected_host["id"]
    host_alias = selected_host["ssh_host"]
    transport = selected_host.get("transport", "ssh")
    result = (
        localexec.identity()
        if transport == "local"
        else _ssh(
            ["python3", "-c", "import getpass,json,os,socket; print(json.dumps({'user':getpass.getuser(),'hostname':socket.gethostname(),'cwd':os.getcwd()}))"],
            timeout=12,
            device=device_id,
        )
    )
    ok = result["returncode"] == 0
    remote = None
    if ok:
        remote = json.loads(result["stdout"].strip())
        _LAST_SUCCESS_UNIX = time.time()
        _DEVICE_LAST_SUCCESS_UNIX[device_id] = _LAST_SUCCESS_UNIX
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
            "kind": "bounded-local" if transport == "local" else "bounded-ssh",
            "name": NAME,
            "identity": IDENTITY,
            "version": VERSION,
        },
        "remote_host": {
            "device_id": device_id,
            "alias": host_alias,
            "user": None if remote is None else remote.get("user"),
            "hostname": None if remote is None else remote.get("hostname") or remote.get("host"),
            "cwd": None if remote is None else remote.get("cwd"),
        },
        "connectivity": {
            "ssh": ok if transport == "ssh" else None,
            "local": ok if transport == "local" else None,
            "transport": transport,
            "gateway": ok,
            "authenticated": ok,
            "authorized": ok,
            "latency_ms": result["duration_ms"],
            "last_success_unix": _DEVICE_LAST_SUCCESS_UNIX.get(device_id),
        },
        "configured_roots": list(selected_host["roots"]),
        "configured_systemd_units": sorted(all_units(cfg, host_id=device_id)),
        "projects": (
            catalog_projects(cfg)
            if device is None
            else [
                item for item in catalog_projects(cfg)
                if item["host"] == device_id
            ]
        ),
        "core_tools": list(REMOTE_TOOLS),
        "capabilities": cap,
        "error": None if ok else _sanitized_error(result["stderr"] or result["stdout"] or "SSH connection failed"),
    }
    if _contains_secret(payload):
        raise RuntimeError("connection_status refused to return secret-bearing fields")
    _audit("connection_status", ok, {
        "device": device_id,
        "host_alias": host_alias,
        "latency_ms": result["duration_ms"],
    })
    return payload


@server.tool(
    name="list_devices",
    annotations=ToolAnnotations(
        title="List managed devices",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_devices(include_resources: bool = False) -> dict[str, Any]:
    """List configured remote hosts and optionally collect live resource/service inventory."""
    cfg = _config()
    rows: list[dict[str, Any]] = []
    identity_argv = [
        "python3", "-c",
        "import getpass,json,os,socket; print(json.dumps({'user':getpass.getuser(),'hostname':socket.gethostname(),'cwd':os.getcwd()}))",
    ]
    for device_id, host in cfg["hosts"].items():
        transport = host.get("transport", "ssh")
        probe = localexec.identity() if transport == "local" else _ssh(identity_argv, timeout=12, device=device_id)
        online = probe["returncode"] == 0
        remote: dict[str, Any] | None = None
        if online:
            try:
                remote = json.loads(probe["stdout"].strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                online = False
            if online:
                _DEVICE_LAST_SUCCESS_UNIX[device_id] = time.time()
        row = {
            "device_id": device_id,
            "name": device_id,
            "default": device_id == cfg["default_host"],
            "online": online,
            "transport": transport,
            "ssh_host": host["ssh_host"],
            "hostname": None if remote is None else remote.get("hostname") or remote.get("host"),
            "user": None if remote is None else remote.get("user"),
            "latency_ms": probe["duration_ms"],
            "last_seen": _DEVICE_LAST_SUCCESS_UNIX.get(device_id),
            "projects": [
                project["name"] for project in catalog_projects(cfg)
                if project["host"] == device_id
            ],
            "capabilities": (
                ["filesystem", "git", "exec", "durable_jobs"]
                if transport == "local"
                else ["filesystem", "git", "exec", "process", "systemd", "logs", "durable_jobs"]
            ),
            "error": None if online else _sanitized_error(
                probe["stderr"] or probe["stdout"] or "SSH connection failed"
            ),
        }
        if include_resources:
            row["inventory"] = (
                _inventory_for_device(cfg, device_id)
                if online
                else {"ok": False, "error": "device is offline"}
            )
        rows.append(row)
    result = {"default_device": cfg["default_host"], "devices": rows}
    if _contains_secret(result):
        raise RuntimeError("list_devices refused to return secret-bearing fields")
    _audit("list_devices", True, {
        "count": len(rows),
        "online": sum(1 for row in rows if row["online"]),
        "include_resources": include_resources,
    })
    return result


def _recent_audit_events(limit: int = 20) -> list[dict[str, Any]]:
    path = Path.home() / ".livingruntime" / "remote-audit.jsonl"
    if not path.exists():
        return []
    bounded = min(100, max(1, int(limit)))
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 131072))
            raw = fh.read(131072)
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    rows: list[dict[str, Any]] = []
    for line in lines[-bounded:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        rows.append({
            "ts": value.get("ts"),
            "tool": value.get("tool"),
            "ok": bool(value.get("ok")),
        })
    return rows


@server.tool(
    name="remote_overview",
    annotations=ToolAnnotations(
        title="Remote control plane overview",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def remote_overview(include_resources: bool = False) -> dict[str, Any]:
    """Return a compact, secret-free control-plane snapshot for long-running work."""
    devices = list_devices(include_resources=include_resources)
    permission_state = exec_permission_snapshot()
    pending_permissions = []
    for row in permission_state.get("pending") or []:
        argv = row.get("argv") if isinstance(row, dict) else None
        executable = None
        if isinstance(argv, list) and argv:
            executable = os.path.basename(str(argv[0]))
        pending_permissions.append({
            "request_id": row.get("request_id"),
            "host_id": row.get("host_id"),
            "project": row.get("project"),
            "risk": row.get("risk"),
            "executable": executable,
        })
    jobs = list_jobs(limit=25)["jobs"]
    cognition_status = None
    cognition_request_id = tracked_cognition_request_id()
    if cognition_request_id:
        try:
            cognition_status = get_cognition_request_status(cognition_request_id)
        except (OSError, ValueError, RuntimeError):
            cognition_status = None
    execution = execution_status_snapshot(
        jobs=jobs,
        cognition_status=cognition_status,
    )
    credential_rows = credential_snapshot()
    credentials = [
        {
            "handle": row.get("handle"),
            "provider": row.get("provider"),
            "capabilities": row.get("capabilities") or [],
            "projects": row.get("projects") or [],
            "devices": row.get("devices") or [],
            "configured": bool(
                row.get("secret_present") and row.get("secret_permissions_ok")
            ),
        }
        for row in credential_rows
    ]
    leases = credential_lease_snapshot(active_only=True)
    completion_events = []
    for job in jobs:
        event = job.get("completion_event") if isinstance(job, dict) else None
        if not isinstance(event, dict) or event.get("acknowledged_at") is not None:
            continue
        completion_events.append({
            "event_id": event.get("event_id"),
            "job_id": job.get("job_id"),
            "status": event.get("status") or job.get("status"),
            "terminal": bool(event.get("terminal")),
            "created_at": event.get("created_at"),
            "delivery_state": event.get("delivery_state") or "PENDING",
            "delivery_attempts": int(event.get("delivery_attempts") or 0),
            "last_delivery_at": event.get("last_delivery_at"),
            "claimed_by_session": event.get("claimed_by_session"),
            "claim_expires_at": event.get("claim_expires_at"),
            "delivered_at": event.get("delivered_at"),
            "goal": job.get("goal"),
            "project": job.get("project"),
            "device": job.get("device"),
        })
    result = {
        "version": VERSION,
        "generated_at": time.time(),
        "devices": devices,
        "jobs": jobs,
        "execution": execution,
        "permissions": {
            "pending": pending_permissions,
            "active_count": len(permission_state.get("grants") or []),
            "revoked_count": len(permission_state.get("revoked") or []),
        },
        "credentials": credentials,
        "active_credential_leases": leases,
        "completion_events": completion_events,
        "activity": _recent_audit_events(20),
    }
    if _contains_secret(result):
        raise RuntimeError("remote_overview refused to return secret-bearing fields")
    _audit("remote_overview", True, {
        "devices": len(devices.get("devices") or []),
        "jobs": len(jobs),
        "pending_permissions": len(permission_state.get("pending") or []),
        "credentials": len(credentials),
        "include_resources": include_resources,
    })
    return result


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
    device: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    detached: bool = False,
    heartbeat_seconds: int = 10,
    stall_seconds: int = 300,
) -> dict[str, Any]:
    """Run a bounded command or return a durable operator approval request.

    Built-in development executables remain immediately available. Other commands
    require a persisted grant. Grants are host-scoped by default; only read-only
    diagnostics can be approved across all owned hosts. Detached execution always
    requires an exact host-scoped approval and returns after the child is spawned.
    """
    _validate_exec(argv)
    requested_timeout = max(1, int(timeout_seconds))
    if not detached and requested_timeout > 30:
        _audit("exec_long_job_required", True, {
            "argv": list(argv),
            "project": project,
            "device": device,
            "requested_timeout_seconds": requested_timeout,
        })
        return {
            "ok": False,
            "long_job_required": True,
            "suggested_tool": "start_long_job",
            "reason": (
                "Synchronous exec is capped at 30 seconds so a model turn cannot "
                "silently block on long work. Start this command with start_long_job "
                "to get heartbeat, stall detection, durable receipt, and watcher UI."
            ),
            "requested_timeout_seconds": requested_timeout,
            "argv": list(argv),
            "project": project,
            "device": device,
        }
    timeout = min(30, requested_timeout)
    heartbeat = min(60, max(1, int(heartbeat_seconds)))
    stall = min(86400, max(heartbeat * 2, int(stall_seconds)))
    cfg = _config()
    selected_host = host_for(cfg, project=project, host_id=device)
    host_id = selected_host["id"]
    workdir = resolve_path(cfg, cwd, project=project, host_id=device) if (cwd or project) else _roots(project, device)[0]
    executable = os.path.basename(str(argv[0]))
    if detached or executable not in _ALLOWED_EXECUTABLES:
        grant = dynamic_exec_grant(
            host_id=host_id,
            project=project,
            cwd=workdir,
            argv=argv,
            detached=detached,
        )
        if grant is None:
            request = ensure_dynamic_exec_request(
                host_id=host_id,
                project=project,
                cwd=workdir,
                argv=argv,
                detached=detached,
            )
            _audit("exec_permission_request", True, {
                "request_id": request["request_id"],
                "host_id": host_id,
                "project": project,
                "argv": argv,
                "risk": request["risk"],
            })
            return {
                "ok": False,
                "approval_required": True,
                "request": {
                    "request_id": request["request_id"],
                    "host_id": host_id,
                    "project": project,
                    "cwd": workdir,
                    "argv": list(argv),
                    "detached": bool(detached),
                    "risk": request["risk"],
                    "allowed_scopes": (
                        ["host", "all_owned_hosts"]
                        if request["risk"] == "read_only_diagnostic"
                        else ["host"]
                    ),
                    "allowed_grant_modes": (
                        ["exact", "diagnostic_class"]
                        if request["risk"] == "read_only_diagnostic"
                        else ["exact"]
                    ),
                },
            }
    execution_id = (
        _detached_execution_id(
            host_id=host_id, project=project, cwd=workdir, argv=argv
        )
        if detached
        else None
    )
    try:
        result = _remote("exec", {
            "argv": argv, "cwd": workdir,
            "timeout": timeout, "max_output": MAX_OUTPUT_BYTES,
            "detached": bool(detached),
            "execution_id": execution_id,
            "heartbeat_seconds": heartbeat,
            "stall_seconds": stall,
            "tail_bytes": 32768,
            "worker_code": _DETACHED_EXEC_WORKER if detached else None,
        }, timeout=timeout + 2, project=project, device=device)
    except Exception as exc:
        if not detached or execution_id is None:
            raise
        try:
            receipt = _remote(
                "exec_receipt",
                {"execution_id": execution_id},
                timeout=5,
                project=project,
                device=device,
            )
        except Exception:
            _audit("exec_detached_ambiguous", False, {
                "execution_id": execution_id,
                "argv": argv,
                "project": project,
                "device": device,
                "error_type": type(exc).__name__,
            })
            raise exc
        if not receipt.get("found"):
            raise exc
        result = dict(receipt)
        result.pop("found", None)
        result["recovered_after_transport_error"] = True
    _audit("exec", True, {
        "argv": argv, "cwd": result.get("cwd"), "project": project,
        "device": device, "returncode": result.get("returncode"),
        "detached": bool(detached), "pid": result.get("pid"),
        "execution_id": result.get("execution_id"),
        "recovered_after_transport_error": bool(
            result.get("recovered_after_transport_error")
        ),
    })
    return result


@server.tool(
    name="list_exec_permissions",
    annotations=ToolAnnotations(
        title="List dynamic exec permissions",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_exec_permissions() -> dict[str, Any]:
    """List pending dynamic-exec requests plus active and revoked grants."""
    result = exec_permission_snapshot()
    _audit("list_exec_permissions", True, {
        "pending": len(result["pending"]),
        "grants": len(result["grants"]),
        "revoked": len(result["revoked"]),
    })
    return result


@server.tool(
    name="approve_exec_permission",
    annotations=ToolAnnotations(
        title="Approve dynamic exec permission",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def approve_exec_permission(
    request_id: str,
    scope: str = "host",
    grant_mode: str = "exact",
) -> dict[str, Any]:
    """Approve a pending exec request.

    scope=host is the default. all_owned_hosts and diagnostic_class are accepted
    only for commands classified as read-only diagnostics.
    """
    grant = approve_dynamic_exec(
        request_id,
        scope=scope,
        grant_mode=grant_mode,
        operator="mcp_operator",
    )
    _audit("approve_exec_permission", True, {
        "request_id": request_id,
        "permission_id": grant["permission_id"],
        "scope": grant["scope"],
        "grant_mode": grant["grant_mode"],
        "risk": grant["risk"],
    })
    return grant


@server.tool(
    name="deny_exec_permission",
    annotations=ToolAnnotations(
        title="Deny dynamic exec permission",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def deny_exec_permission(request_id: str) -> dict[str, Any]:
    """Deny one pending dynamic-exec request."""
    request = deny_dynamic_exec(request_id, operator="mcp_operator")
    _audit("deny_exec_permission", True, {
        "request_id": request_id,
        "host_id": request.get("host_id"),
        "argv": request.get("argv"),
    })
    return request


@server.tool(
    name="revoke_exec_permission",
    annotations=ToolAnnotations(
        title="Revoke dynamic exec permission",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revoke_exec_permission(permission_id: str) -> dict[str, Any]:
    """Revoke a previously persisted dynamic-exec grant."""
    grant = revoke_dynamic_exec(permission_id, operator="mcp_operator")
    _audit("revoke_exec_permission", True, {
        "permission_id": permission_id,
        "scope": grant.get("scope"),
        "risk": grant.get("risk"),
    })
    return grant


@server.tool(
    name="list_credentials",
    annotations=ToolAnnotations(
        title="List credential handles",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_credentials() -> dict[str, Any]:
    """List local credential handles and scopes without reading or returning secret values."""
    rows = credential_snapshot()
    _audit("list_credentials", True, {"count": len(rows)})
    return {"credentials": rows}


@server.tool(
    name="lease_credential",
    annotations=ToolAnnotations(
        title="Lease scoped credential handle",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def lease_credential(
    handle: str,
    capability: str,
    project: str | None = None,
    device: str | None = None,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Issue a short-lived opaque lease for a credential handle.

    The secret itself is never returned. A later dedicated capability may consume
    the lease internally after checking the same capability/project/device scope.
    """
    cfg = _config()
    resolved_device = device
    if project is not None or device is not None:
        selected = host_for(cfg, project=project, host_id=device)
        resolved_device = selected["id"]
    lease = create_credential_lease(
        handle,
        capability=capability,
        project=project,
        device=resolved_device,
        ttl_seconds=ttl_seconds,
        issued_to="mcp_model",
    )
    _audit("lease_credential", True, {
        "lease_id": lease["lease_id"],
        "handle": lease["handle"],
        "capability": lease["capability"],
        "project": lease.get("project"),
        "device": lease.get("device"),
        "expires_at": lease["expires_at"],
    })
    return lease


@server.tool(
    name="list_credential_leases",
    annotations=ToolAnnotations(
        title="List credential leases",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_credential_leases(active_only: bool = True) -> dict[str, Any]:
    """List opaque credential leases; never returns credential values."""
    rows = credential_lease_snapshot(active_only=active_only)
    _audit("list_credential_leases", True, {
        "active_only": active_only,
        "count": len(rows),
    })
    return {"leases": rows}


@server.tool(
    name="revoke_credential_lease",
    annotations=ToolAnnotations(
        title="Revoke credential lease",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revoke_credential_lease(lease_id: str) -> dict[str, Any]:
    """Revoke one opaque credential lease without deleting the underlying credential."""
    result = revoke_credential_lease_runtime(lease_id, operator="mcp_operator")
    _audit("revoke_credential_lease", True, {
        "lease_id": lease_id,
        "handle": result.get("handle"),
        "capability": result.get("capability"),
    })
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("credential verification redirects are not allowed")


@server.tool(
    name="github_identity",
    annotations=ToolAnnotations(
        title="Verify GitHub credential identity",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=True,
    ),
)
def github_identity(
    lease_id: str,
    project: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """Use a scoped GitHub credential lease internally without exposing its secret."""
    cfg = _config()
    resolved_device = device
    if project is not None or device is not None:
        selected = host_for(cfg, project=project, host_id=device)
        resolved_device = selected["id"]
    secret = resolve_secret_for_lease(
        lease_id,
        capability="github.identity",
        project=project,
        device=resolved_device,
        provider="github",
    )
    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "authorization": f"Bearer {secret}",
            "accept": "application/vnd.github+json",
            "user-agent": f"LivingRuntime-Remote/{VERSION}",
            "x-github-api-version": "2022-11-28",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read(65537)
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub credential verification failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("GitHub credential verification failed") from None
    if status != 200:
        raise RuntimeError(f"GitHub credential verification failed with HTTP {status}")
    if len(body) > 65536:
        raise RuntimeError("GitHub identity response exceeded size limit")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("GitHub identity response was invalid") from None
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub identity response was invalid")
    result = {
        "provider": "github",
        "authenticated": True,
        "login": payload.get("login"),
        "id": payload.get("id"),
        "name": payload.get("name"),
        "type": payload.get("type"),
    }
    _audit("github_identity", True, {
        "lease_id": lease_id,
        "project": project,
        "device": resolved_device,
        "login": result["login"],
    })
    return result


@server.tool(
    name="create_job",
    annotations=ToolAnnotations(
        title="Create durable Remote job",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def create_job(
    goal: str,
    project: str | None = None,
    device: str | None = None,
    pi_job_id: str | None = None,
    pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
    pi_job_root: str | None = None,
) -> dict[str, Any]:
    """Create a durable LivingRuntime job, optionally linked to an existing Pi job."""
    cfg = _config()
    if project is not None or device is not None:
        host_for(cfg, project=project, host_id=device)
    backend = None
    if pi_job_id is not None:
        backend = {
            "type": "pi",
            "job_id": pi_job_id,
            "pi_remote_dir": pi_remote_dir,
            "job_root": pi_job_root,
        }
    result = create_runtime_job(
        goal=goal,
        project=project,
        device=device,
        backend=backend,
    )
    _audit("create_job", True, {
        "job_id": result["job_id"],
        "project": project,
        "device": device,
        "backend_type": None if backend is None else backend["type"],
    })
    return result


@server.tool(
    name="get_job",
    annotations=ToolAnnotations(
        title="Get durable Remote job",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def get_job(job_id: str) -> dict[str, Any]:
    """Read one durable job including goal, current step, next action and checkpoints."""
    result = _reconcile_runtime_job(get_runtime_job(job_id))
    _audit("get_job", True, {"job_id": job_id, "status": result.get("status")})
    return result


@server.tool(
    name="list_jobs",
    annotations=ToolAnnotations(
        title="List durable Remote jobs",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_jobs(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List durable jobs after reconciling supervised exec receipts."""
    bounded_limit = min(200, max(1, int(limit)))
    scan_limit = 200 if status is not None else bounded_limit
    rows = [
        _reconcile_runtime_job(row)
        for row in runtime_jobs_snapshot(limit=scan_limit)
    ]
    rows.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
    if status is not None:
        rows = [row for row in rows if row.get("status") == status]
    rows = rows[:bounded_limit]
    _audit("list_jobs", True, {"status": status, "count": len(rows)})
    return {"jobs": rows}


@server.tool(
    name="checkpoint_job",
    annotations=ToolAnnotations(
        title="Checkpoint durable Remote job",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def checkpoint_job(
    job_id: str,
    summary: str,
    current_step: str | None = None,
    next_action: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Persist progress so another model turn or session can resume the same goal."""
    result = checkpoint_runtime_job(
        job_id,
        summary=summary,
        current_step=current_step,
        next_action=next_action,
        status=status,
        source="model",
    )
    _audit("checkpoint_job", True, {
        "job_id": job_id,
        "status": result.get("status"),
        "checkpoint_count": len(result.get("checkpoints") or []),
    })
    return result


@server.tool(
    name="submit_llm_request",
    annotations=ToolAnnotations(
        title="Submit durable LLM request",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def submit_llm_request(
    agent_id: str,
    purpose: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    timeout_seconds: int = 300,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Persist an LLM request for a ChatGPT cognition watcher."""
    result = submit_cognition_request(
        agent_id=agent_id,
        purpose=purpose,
        messages=messages,
        tools=tools,
        response_format=response_format,
        options=options,
        metadata=metadata,
        timeout_seconds=timeout_seconds,
        request_id=request_id,
    )
    _audit("submit_llm_request", True, {
        "request_id": result["request_id"],
        "agent_id": result["agent_id"],
        "purpose": result["purpose"],
        "status": result["status"],
        "activation_status": (result.get("activation") or {}).get("status"),
        "activation_transport": (result.get("activation") or {}).get("transport"),
    })
    return result


@server.tool(
    name="watch_agent_cognition",
    annotations=ToolAnnotations(
        title="Watch agent cognition requests",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def watch_agent_cognition(agent_id: str) -> dict[str, Any]:
    """Describe the native event contract; only the public relay can verify subscriptions."""
    agent_id = str(agent_id).strip()
    watcher_id = "chatgpt-work-ferro-cortex" if agent_id == "ferro" else cognition_watcher_id(agent_id)
    result = {
        "agentId": str(agent_id).strip(),
        "watcherId": watcher_id,
        "watchRecommended": True,
        "status": "SUBSCRIPTION_REQUIRED",
        "watcherState": "SUBSCRIPTION_REQUIRED",
        "eventName": "cognition.requested",
        "eventArguments": {"agent_id": agent_id},
        "subscriptionVerified": False,
        "polling": False,
        "autoClaimed": False,
    }
    _audit("watch_agent_cognition", True, {
        "agent_id": result["agentId"],
        "watcher_id": watcher_id,
    })
    return result


@server.tool(
    name="wait_llm_request",
    annotations=ToolAnnotations(
        title="Wait for agent LLM request",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def wait_llm_request(
    agent_id: str,
    watcher_id: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """App-side bounded read-only wait for one pending cognition request."""
    resolved_watcher_id = str(watcher_id or "").strip() or cognition_watcher_id(agent_id)
    result = wait_pending_cognition_request(
        agent_id=agent_id,
        watcher_id=resolved_watcher_id,
        timeout_seconds=timeout_seconds,
    )
    result["watcherId"] = resolved_watcher_id
    result["watcherState"] = (
        "WAITING_FOR_CHATGPT_SESSION"
        if isinstance(result.get("request"), dict)
        else "ARMED"
    )
    request = result.get("request")
    _audit("wait_llm_request", True, {
        "agent_id": agent_id,
        "watcher_id": resolved_watcher_id,
        "request_id": None if not isinstance(request, dict) else request.get("request_id"),
        "timed_out": bool(result.get("timed_out")),
    })
    return result


@server.tool(
    name="claim_llm_request",
    annotations=ToolAnnotations(
        title="Claim durable LLM request",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def claim_llm_request(
    request_id: str,
    watcher_id: str,
    claim_seconds: int = 120,
) -> dict[str, Any]:
    """Claim one request after a cognition watcher wakes ChatGPT."""
    result = claim_cognition_request(
        request_id=request_id,
        watcher_id=watcher_id,
        claim_seconds=claim_seconds,
    )
    _audit("claim_llm_request", True, {
        "request_id": request_id,
        "watcher_id": watcher_id,
        "status": result.get("status"),
        "attempts": result.get("attempts"),
    })
    return result


@server.tool(
    name="get_llm_request",
    annotations=ToolAnnotations(
        title="Get durable LLM request",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def get_llm_request(request_id: str) -> dict[str, Any]:
    """Read a durable cognition request including its prompt and current claim."""
    result = get_cognition_request(request_id)
    _audit("get_llm_request", True, {
        "request_id": request_id,
        "status": result.get("status"),
    })
    return result


@server.tool(
    name="get_llm_request_status",
    annotations=ToolAnnotations(
        title="Get LLM request status",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def get_llm_request_status(request_id: str) -> dict[str, Any]:
    """Read a bounded status/provenance summary without returning the prompt."""
    result = get_cognition_request_status(request_id)
    _audit("get_llm_request_status", True, {
        "request_id": request_id,
        "status": result.get("status"),
    })
    return result


def _normalize_cognition_response_text(
    value: str | dict[str, Any] | list[Any] | int | float | bool | None,
) -> str | None:
    """Preserve text responses and serialize structured model output as JSON."""
    if value is None or isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("response_text must be JSON-serializable") from exc


@server.tool(
    name="complete_llm_request",
    annotations=ToolAnnotations(
        title="Complete durable LLM request",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def complete_llm_request(
    request_id: str,
    claim_token: str,
    response_text: str | dict[str, Any] | list[Any] | int | float | bool | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Complete one request and immediately claim the next turn in its lane."""
    current = get_cognition_request(request_id)
    prior_claim = current.get("claim") if isinstance(current.get("claim"), dict) else {}
    prior_watcher_id = str(prior_claim.get("watcher_id") or "").strip()
    result = complete_cognition_request(
        request_id=request_id,
        response_text=_normalize_cognition_response_text(response_text),
        tool_calls=tool_calls,
        claim_token=claim_token,
        provider="livingruntime-chatgpt",
        model=model,
        session_id=session_id,
    )
    _audit("complete_llm_request", True, {
        "request_id": request_id,
        "status": result.get("status"),
        "model": model,
    })
    agent_id = str(result.get("agent_id") or current.get("agent_id") or "").strip()
    if not agent_id:
        return result
    watcher_id = prior_watcher_id or cognition_watcher_id(agent_id)
    next_request = claim_next_cognition_request(
        agent_id=agent_id,
        watcher_id=watcher_id,
        claim_seconds=300,
        lane=cognition_dispatch_lane(current),
    )
    if not isinstance(next_request, dict) or not next_request.get("request_id"):
        return {
            **result,
            "agentId": agent_id,
            "watcherId": watcher_id,
            "watchRecommended": True,
            "watcherState": "SUBSCRIPTION_UNVERIFIED",
            "watcherAutoRearmed": False,
            "nextRequestAutoClaimed": False,
            "completedRequestId": request_id,
        }
    claimed = next_request
    return {
        **claimed,
        "agentId": agent_id,
        "watcherId": watcher_id,
        "watchRecommended": True,
        "watcherState": "WAITING_FOR_CHATGPT_SESSION",
        "watcherAutoRearmed": False,
        "nextRequestAutoClaimed": True,
        "completedRequestId": request_id,
    }



def _long_job_runtime(receipt: dict[str, Any]) -> dict[str, Any]:
    status = str(receipt.get("status") or "UNKNOWN").upper()
    observed = str(receipt.get("observed_status") or status).upper()
    live = bool(receipt.get("worker_alive")) or bool(receipt.get("child_alive"))
    runtime: dict[str, Any] = {
        "backend_status": status,
        "observed_status": observed,
        "progress_state": (
            "STALLED"
            if observed in {"STALLED", "HEARTBEAT_STALE", "LOST", "ORPHANED"}
            else (
                "TERMINAL"
                if bool(receipt.get("terminal"))
                else ("ACTIVE" if live else "WAITING")
            )
        ),
    }
    for key in (
        "last_heartbeat_at", "last_progress_at", "heartbeat_age_seconds",
        "progress_age_seconds", "started_at", "finished_at", "stdout_bytes",
        "stderr_bytes", "worker_pid", "child_pid", "returncode",
        "worker_alive", "child_alive",
    ):
        value = receipt.get(key)
        if value is None:
            continue
        target = "pid" if key == "worker_pid" else key
        if isinstance(value, bool):
            runtime[target] = value
        elif isinstance(value, (int, float)):
            runtime[target] = value
    error = receipt.get("error")
    if error:
        runtime["error"] = str(error)[:2000]
    return runtime


def _long_job_status(receipt: dict[str, Any]) -> str:
    observed = str(
        receipt.get("observed_status") or receipt.get("status") or ""
    ).upper()
    if observed in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return observed
    if observed in {"STALLED", "HEARTBEAT_STALE", "LOST", "ORPHANED"}:
        return "STALLED"
    if bool(receipt.get("worker_alive")) or bool(receipt.get("child_alive")):
        return "RUNNING"
    if observed in {"STARTING", "STARTED", "PENDING", "QUEUED", ""}:
        return "PENDING"
    return "WAITING"


def _sync_long_job(job: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    mapped = _long_job_status(receipt)
    previous = str(job.get("status") or "PENDING")
    execution_id = str((job.get("backend") or {}).get("job_id") or "")
    current_step = {
        "STALLED": "Detached command stalled",
        "RUNNING": "Detached command running",
        "PENDING": "Detached command queued",
        "WAITING": "Detached command waiting",
        "SUCCEEDED": "Detached command succeeded",
        "FAILED": "Detached command failed",
        "CANCELLED": "Detached command cancelled",
    }.get(mapped, f"Detached command {mapped.lower()}")

    job = update_runtime_job(
        job["job_id"],
        runtime=_long_job_runtime(receipt),
        status=mapped,
        current_step=current_step,
    )

    if previous != mapped and previous not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        if mapped == "STALLED":
            summary = (
                f"Detached execution {execution_id} has no observable progress "
                f"or heartbeat within its configured threshold."
            )
            next_action = (
                "Inspect the durable output tail and liveness fields; cancel or retry "
                "only after identifying whether the command is actually hung."
            )
        elif mapped == "RUNNING":
            summary = f"Detached execution {execution_id} has a live server process."
            next_action = "Keep the watcher attached until terminal state."
        elif mapped == "PENDING":
            summary = (
                f"Detached execution {execution_id} is accepted but no live server process "
                "is observed yet."
            )
            next_action = "Keep the watcher armed and wait for a live-process receipt."
        elif mapped == "WAITING":
            summary = (
                f"Detached execution {execution_id} has no live server process and is waiting."
            )
            next_action = "Inspect the durable receipt before deciding whether to resume or retry."
        else:
            summary = f"Detached execution {execution_id} ended with status {mapped}."
            next_action = (
                "Inspect stdout/stderr tail and acceptance evidence, then continue the goal."
            )
        job = checkpoint_runtime_job(
            job["job_id"],
            summary=summary,
            current_step=current_step,
            next_action=next_action,
            status=mapped,
            source="backend",
        )
    return job


@server.tool(
    name="start_long_job",
    annotations=ToolAnnotations(
        title="Start supervised long-running command",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def start_long_job(
    goal: str,
    argv: list[str],
    cwd: str | None = None,
    project: str | None = None,
    device: str | None = None,
    stall_seconds: int = 300,
    heartbeat_seconds: int = 10,
) -> dict[str, Any]:
    """Start a command under a durable detached supervisor and return immediately."""
    goal = str(goal).strip()
    if not goal or len(goal) > 8000:
        raise ValueError("goal must be a bounded non-empty string")
    launched = exec(
        argv=argv,
        cwd=cwd,
        project=project,
        device=device,
        timeout_seconds=30,
        detached=True,
        heartbeat_seconds=heartbeat_seconds,
        stall_seconds=stall_seconds,
    )
    if launched.get("approval_required"):
        return {
            **launched,
            "long_job_started": False,
            "goal": goal,
        }

    execution_id = str(launched.get("execution_id") or "")
    if not execution_id:
        raise RuntimeError("detached exec returned no execution id")
    cfg = _config()
    selected = host_for(cfg, project=project, host_id=device)
    workdir = str(launched.get("cwd") or resolve_path(cfg, cwd, project=project, host_id=device))
    runtime_job = create_runtime_job(
        goal=goal,
        project=project,
        device=selected["id"],
        backend={
            "type": "exec",
            "job_id": execution_id,
            "cwd": workdir,
            "executable": os.path.basename(str(argv[0])),
        },
        status="PENDING",
    )
    runtime_job = checkpoint_runtime_job(
        runtime_job["job_id"],
        summary=f"Supervised detached execution {execution_id} was accepted.",
        current_step=f"Queued {os.path.basename(str(argv[0]))}",
        next_action="Keep the long-job watcher armed until a live-process receipt arrives.",
        status="PENDING",
        source="runtime",
    )
    receipt = dict(launched)
    receipt["observed_status"] = str(receipt.get("status") or "STARTING")
    runtime_job = _sync_long_job(runtime_job, receipt)
    _audit("start_long_job", True, {
        "runtime_job_id": runtime_job["job_id"],
        "execution_id": execution_id,
        "project": project,
        "device": selected["id"],
        "executable": os.path.basename(str(argv[0])),
    })
    return {
        "runtimeJobId": runtime_job["job_id"],
        "executionId": execution_id,
        "state": receipt,
        "status": runtime_job["status"],
        "terminal": bool(runtime_job["terminal"]),
        "watchRecommended": True,
    }


def _long_job_receipt(job: dict[str, Any]) -> dict[str, Any]:
    backend = job.get("backend")
    if not isinstance(backend, dict) or backend.get("type") != "exec":
        raise ValueError("job is not backed by a supervised exec")
    execution_id = str(backend.get("job_id") or "")
    receipt = _remote(
        "exec_receipt",
        {"execution_id": execution_id},
        timeout=8,
        project=job.get("project"),
        device=job.get("device"),
    )
    if not receipt.get("found"):
        raise RuntimeError("detached execution receipt is missing")
    return receipt


def _reconcile_runtime_job(job: dict[str, Any]) -> dict[str, Any]:
    """Repair stale durable state from the authoritative backend receipt."""
    if bool(job.get("terminal")):
        return job
    backend = job.get("backend")
    if not isinstance(backend, dict):
        return job
    try:
        if backend.get("type") == "exec":
            return _sync_long_job(job, _long_job_receipt(job))
        if backend.get("type") in {"pi", "pi-agent", "pi-step"}:
            state = _pi_job_command(
                "job-status",
                str(backend.get("job_id") or ""),
                str(backend.get("pi_remote_dir") or "/home/ubuntu/src/pi-remote"),
                backend.get("job_root"),
                timeout_seconds=15,
            )
            return _sync_pi_runtime_job(job, state)
        return job
    except Exception as exc:
        result = dict(job)
        runtime = dict(result.get("runtime") or {})
        runtime["reconcile_error"] = str(exc)[:2000]
        result["runtime"] = runtime
        return result


@server.tool(
    name="get_long_job",
    annotations=ToolAnnotations(
        title="Get supervised long-running job",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_long_job(job_id: str) -> dict[str, Any]:
    """Refresh a job receipt without consuming its completion notification."""
    job = get_runtime_job(job_id)
    receipt = _long_job_receipt(job)
    job = _sync_long_job(job, receipt)
    return {
        "job": job,
        "state": receipt,
        "terminal": bool(job.get("terminal")),
    }


@server.tool(
    name="watch_long_job",
    annotations=ToolAnnotations(
        title="Watch supervised long-running job",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def watch_long_job(job_id: str) -> dict[str, Any]:
    """Refresh a supervised long job and return watcher-ready state."""
    return get_long_job(job_id)


@server.tool(
    name="claim_long_job_completion",
    annotations=ToolAnnotations(
        title="Claim long-job completion delivery",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def claim_long_job_completion(
    job_id: str,
    event_id: str,
    session_id: str,
    claim_seconds: int = 300,
) -> dict[str, Any]:
    """Lease one durable completion for a specific UI/session delivery attempt."""
    result = claim_runtime_completion(
        job_id,
        event_id,
        session_id=session_id,
        claim_seconds=claim_seconds,
    )
    _audit("claim_long_job_completion", True, {
        "job_id": job_id,
        "event_id": event_id,
        "session_id": session_id,
        "claimed": bool(result.get("claimed")),
        "reason": result.get("reason"),
    })
    return result


@server.tool(
    name="mark_long_job_completion_delivered",
    annotations=ToolAnnotations(
        title="Mark long-job completion delivered",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def mark_long_job_completion_delivered(
    job_id: str,
    event_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Record successful handoff while leaving acknowledgement to the model."""
    job = mark_runtime_completion_delivered(
        job_id,
        event_id,
        session_id=session_id,
    )
    _audit("mark_long_job_completion_delivered", True, {
        "job_id": job_id,
        "event_id": event_id,
        "session_id": session_id,
    })
    return {"job": job, "completion_event": job.get("completion_event")}


@server.tool(
    name="ack_long_job_completion",
    annotations=ToolAnnotations(
        title="Acknowledge long-job completion",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def ack_long_job_completion(
    job_id: str,
    event_id: str,
    acknowledged_by: str = "chatgpt",
) -> dict[str, Any]:
    """Mark one durable completion event consumed after ChatGPT handles it."""
    job = acknowledge_runtime_completion(
        job_id,
        event_id,
        acknowledged_by=acknowledged_by,
    )
    cleared_bindings = _clear_openai_continuations_for_job(job_id)
    _audit("ack_long_job_completion", True, {
        "job_id": job_id,
        "event_id": event_id,
        "cleared_continuation_bindings": cleared_bindings,
    })
    return {
        "job": job,
        "completion_event": job.get("completion_event"),
        "clearedContinuationBindings": cleared_bindings,
    }


@server.tool(
    name="wait_long_job",
    annotations=ToolAnnotations(
        title="Wait for supervised long-running job",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def wait_long_job(job_id: str, timeout_seconds: int = 30) -> dict[str, Any]:
    """Bounded event wait used by the Apps SDK watcher, not model-side polling."""
    deadline = time.monotonic() + min(90, max(1, int(timeout_seconds)))
    latest: dict[str, Any] | None = None
    while True:
        latest = get_long_job(job_id)
        job = latest["job"]
        if job.get("terminal") or job.get("status") == "STALLED":
            return {**latest, "timedOut": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**latest, "timedOut": True}
        time.sleep(min(1.0, remaining))


@server.tool(
    name="bind_openai_job_continuation",
    annotations=ToolAnnotations(
        title="Bind durable job to OpenAI session",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def bind_openai_job_continuation(
    session_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Bind any durable runtime job to the current OpenAI session."""
    session_id = str(session_id).strip()
    job_id = str(job_id).strip()
    if not session_id or len(session_id) > 256:
        raise ValueError("session_id must be a bounded non-empty string")
    if not job_id or len(job_id) > 128:
        raise ValueError("job_id must be a bounded non-empty string")
    job = _reconcile_runtime_job(get_runtime_job(job_id))
    _save_openai_continuation(session_id, {
        "kind": "runtime-job",
        "session_id": session_id,
        "runtime_job_id": job_id,
        "remaining_continuations": 1,
        "updated_at": time.time(),
    })
    event = (
        job.get("completion_event")
        if isinstance(job.get("completion_event"), dict)
        else None
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"LivingRuntime durable job continuation is armed for {job_id}. "
                f"Current status: {job.get('status')}."
            ),
        },
        "runtimeJobId": job_id,
        "goalTerminal": bool(job.get("terminal")),
        "completionEventId": None if event is None else event.get("event_id"),
    }


@server.tool(
    name="cancel_long_job",
    annotations=ToolAnnotations(
        title="Cancel supervised long-running job",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def cancel_long_job(job_id: str) -> dict[str, Any]:
    """Request cancellation of a supervised command process group."""
    job = get_runtime_job(job_id)
    backend = job.get("backend")
    if not isinstance(backend, dict) or backend.get("type") != "exec":
        raise ValueError("job is not backed by a supervised exec")
    execution_id = str(backend.get("job_id") or "")
    result = _remote(
        "exec_cancel",
        {"execution_id": execution_id},
        timeout=8,
        project=job.get("project"),
        device=job.get("device"),
    )
    _audit("cancel_long_job", True, {
        "runtime_job_id": job_id,
        "execution_id": execution_id,
        "cancel_requested": bool(result.get("cancel_requested")),
    })
    return {
        "job": get_runtime_job(job_id),
        "cancel": result,
    }


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


def _clear_openai_continuations_for_job(job_id: str) -> int:
    root = Path.home() / ".livingruntime" / "openai-continuations"
    if not root.exists():
        return 0
    cleared = 0
    for path in root.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if (
            value.get("kind") == "runtime-job"
            and str(value.get("runtime_job_id") or "") == str(job_id)
        ):
            try:
                path.unlink()
                cleared += 1
            except FileNotFoundError:
                pass
    return cleared


@server.tool(
    name="recover_openai_job_continuation",
    annotations=ToolAnnotations(
        title="Recover durable OpenAI job continuation",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def recover_openai_job_continuation(
    session_id: str,
    hook_event_name: str = "SessionStart",
) -> dict[str, Any]:
    """Inject a still-bound durable job back into a resumed OpenAI session."""
    session_id = str(session_id).strip()
    if not session_id or len(session_id) > 256:
        raise ValueError("session_id must be a bounded non-empty string")
    event_name = str(hook_event_name or "SessionStart").strip()
    if event_name not in {"SessionStart", "UserPromptSubmit"}:
        raise ValueError("hook_event_name must be SessionStart or UserPromptSubmit")
    binding = _load_openai_continuation(session_id)
    if binding is None or binding.get("kind") != "runtime-job":
        return {"continue": True}
    runtime_job_id = str(binding.get("runtime_job_id") or "").strip()
    if not runtime_job_id:
        return {"continue": True}
    latest = _reconcile_runtime_job(get_runtime_job(runtime_job_id))
    completion = (
        latest.get("completion_event")
        if isinstance(latest.get("completion_event"), dict)
        else {}
    )
    if latest.get("terminal"):
        context = (
            f"LivingRuntime durable job {runtime_job_id} completed with status "
            f"{latest.get('status')}. Durable completion event "
            f"{completion.get('event_id') or 'unknown'} is still bound to this "
            "session. Before finishing this turn, call get_long_job, consume the "
            "terminal evidence, continue or repair the original goal as needed, "
            "then call ack_long_job_completion for that exact event."
        )
    else:
        context = (
            f"LivingRuntime durable job {runtime_job_id} is still bound to this "
            f"session with status {latest.get('status')}. Reattach with "
            "watch_long_job instead of asking the user to type continue or "
            "assuming old UI state is current."
        )
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        },
        "runtimeJobId": runtime_job_id,
        "status": latest.get("status"),
        "terminal": bool(latest.get("terminal")),
        "completionEventId": completion.get("event_id"),
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
    device: str | None = None,
    offset: int = 0,
    max_bytes: int = 131072,
) -> dict[str, Any]:
    """Read a bounded byte range from a file. Prefer project= plus a project-relative path."""
    cfg = _config()
    host_for(cfg, project=project, host_id=device)
    target = resolve_path(cfg, path, project=project, host_id=device)
    result = _remote("read_file", {
        "path": target, "offset": max(0, int(offset)),
        "max_bytes": min(MAX_OUTPUT_BYTES, max(1, int(max_bytes))),
    }, project=project, device=device)
    _audit("read_file", True, {"path": result["path"], "project": project, "device": device, "bytes_read": result["bytes_read"]})
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
    device: str | None = None,
    mode: str = "replace",
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Write UTF-8 text under a configured root or named project, optionally guarded by current SHA-256."""
    if mode not in {"replace", "append"}:
        raise ValueError("mode must be replace or append")
    if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("content exceeds maximum write size")
    cfg = _config()
    host_for(cfg, project=project, host_id=device)
    target = resolve_path(cfg, path, project=project, host_id=device)
    result = _remote("write_file", {
        "path": target, "content": content, "mode": mode,
        "expected_sha256": expected_sha256,
    }, project=project, device=device)
    _audit("write_file", True, {"path": result["path"], "project": project, "device": device, "mode": mode, "bytes": result["bytes"]})
    return result


@server.tool(
    name="delete_path",
    annotations=ToolAnnotations(
        title="Delete remote file or directory",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def delete_path(
    path: str,
    project: str | None = None,
    device: str | None = None,
    recursive: bool = False,
    dry_run: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Delete one literal POSIX path within configured roots. Directories require recursive=true.
    Symlinks are removed without following their targets. Root/project and credential-state
    deletion is refused. At most 10,000 entries / 512 MiB / 64 levels per call.
    dry_run reports the candidate size without mutation; expected_sha256 guards regular files.
    """
    detail = {"path": path, "project": project, "device": device, "dry_run": bool(dry_run)}
    try:
        cfg = _config()
        host = host_for(cfg, project=project, host_id=device)
        target = resolve_path(cfg, path, project=project, host_id=device)
        protected = [item["path"] for item in cfg["projects"].values()
                     if item["host"] == host["id"]]
        result = _remote("delete_path", {
            "path": target, "recursive": bool(recursive), "dry_run": bool(dry_run),
            "expected_sha256": expected_sha256, "protected_paths": protected,
            "delete_worker_source": filesystem_delete.DELETE_WORKER_SOURCE,
        }, project=project, device=device)
    except Exception as exc:
        _audit("delete_path", False, {**detail, "error_type": type(exc).__name__})
        raise
    _audit("delete_path", result["status"] != "PARTIAL_FAILURE", {
        **detail, "path": result["path"], "status": result["status"],
        "deleted_entries": result["deleted_entries"], "deleted_bytes": result["deleted_bytes"],
    })
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
    device: str | None = None,
) -> dict[str, Any]:
    """Apply a unified diff under a configured root. Failure leaves the file unchanged."""
    cfg = _config()
    host_for(cfg, project=project, host_id=device)
    target = resolve_path(cfg, path, project=project, host_id=device)
    current = _remote("read_file", {
        "path": target, "offset": 0, "max_bytes": MAX_OUTPUT_BYTES,
    }, project=project, device=device)
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
    }, project=project, device=device)
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
def list_dir(
    path: str | None = None,
    project: str | None = None,
    device: str | None = None,
    max_entries: int = 200,
) -> dict[str, Any]:
    """List one remote directory under a configured root or named project."""
    cfg = _config()
    host_for(cfg, project=project, host_id=device)
    target = resolve_path(cfg, path, project=project, host_id=device) if (path or project) else _roots(device=device)[0]
    result = _remote("list_dir", {
        "path": target, "max_entries": min(1000, max(1, int(max_entries))),
    }, project=project, device=device)
    _audit("list_dir", True, {"path": result["path"], "project": project, "device": device, "entries": len(result["entries"])})
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
    device: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run a Git subcommand. Prefer project=virtualbrain instead of an absolute repo_path."""
    _validate_git(args)
    timeout = min(120, max(1, int(timeout_seconds)))
    cfg = _config()
    host_for(cfg, project=project, host_id=device)
    cwd = resolve_path(cfg, repo_path, project=project, host_id=device)
    result = _remote("exec", {
        "argv": ["git", *args], "cwd": cwd,
        "timeout": timeout, "max_output": MAX_OUTPUT_BYTES,
    }, timeout=timeout + 2, project=project, device=device)
    _audit("git", True, {"repo_path": cwd, "project": project, "device": device, "args": args, "returncode": result.get("returncode")})
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
def process(
    action: str,
    pid: int | None = None,
    contains: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """List scoped same-user processes or SIGTERM one process whose cwd is under a configured root."""
    selected_host = host_for(_config(), host_id=device)
    if selected_host.get("transport") == "local":
        raise RuntimeError("process inspection is not exposed by the native local transport")
    result = _remote("process", {"action": action, "pid": pid, "contains": contains}, device=device)
    _audit("process", True, {"action": action, "pid": pid, "device": device})
    return result


def _schedule_deferred_systemd_restart(
    selected: str,
    *,
    project: str | None,
    device: str | None,
    root: str,
) -> dict[str, Any]:
    seed = f"{selected}:{time.time_ns()}:{os.getpid()}".encode("utf-8")
    receipt_id = "restart-" + hashlib.sha256(seed).hexdigest()[:20]
    remote = _ssh(
        ["python3", "-c", _DEFERRED_RESTART_SCHEDULER],
        stdin=json.dumps(
            {
                "receipt_id": receipt_id,
                "unit": selected,
                "delay_seconds": DEFERRED_RESTART_DELAY_SECONDS,
                "root": root,
                "worker_code": _DEFERRED_RESTART_WORKER,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        timeout=20,
        project=project,
        device=device,
    )
    receipt: dict[str, Any] = {}
    if remote.get("stdout"):
        try:
            parsed = json.loads(str(remote["stdout"]).strip().splitlines()[-1])
            if isinstance(parsed, dict):
                receipt = parsed
        except json.JSONDecodeError:
            receipt = {}
    if not receipt:
        receipt = {
            "receipt_id": receipt_id,
            "action": "restart",
            "unit": selected,
            "status": "RESTART_SCHEDULE_FAILED",
        }
    return {
        "returncode": int(receipt.get("returncode", remote.get("returncode", 1))),
        "stdout": "",
        "stderr": remote.get("stderr", ""),
        "duration_ms": remote.get("duration_ms"),
        "deferred": True,
        **receipt,
    }


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
def systemd(
    action: str,
    unit: str | None = None,
    project: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """Inspect or control an explicitly allowlisted remote systemd service."""
    cfg = _config()
    selected_host = host_for(cfg, project=project, host_id=device)
    selected = resolve_unit(cfg, unit, project=project, host_id=device)
    if action not in {"status", "is-active", "start", "stop", "restart"}:
        raise ValueError("unsupported systemd action")
    if selected_host.get("transport") == "local":
        result = _remote(
            "systemd",
            {"action": action, "unit": selected},
            project=project,
            device=device,
        )
        _audit("systemd", result["returncode"] == 0, {
            "action": action,
            "unit": selected,
            "project": project,
            "device": device,
            "transport": "local",
        })
        return result
    if action == "restart" and selected in DEFERRED_SELF_RESTART_UNITS:
        result = _schedule_deferred_systemd_restart(
            selected,
            project=project,
            device=device,
            root=selected_host["roots"][0],
        )
        _audit("systemd", result["status"] == "RESTART_SCHEDULED", {
            "action": action,
            "unit": selected,
            "project": project,
            "device": device,
            "deferred": True,
            "receipt_id": result.get("receipt_id"),
            "status": result.get("status"),
        })
        return result
    argv = ["systemctl", action, selected]
    if action in {"start", "stop", "restart"}:
        argv = ["sudo", "-n", *argv]
    result = _ssh(argv, timeout=30, project=project, device=device)
    _audit("systemd", result["returncode"] == 0, {"action": action, "unit": selected, "project": project, "device": device})
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
    device: str | None = None,
    lines: int = 200,
    since_minutes: int = 60,
) -> dict[str, Any]:
    """Read bounded journal logs. Prefer project=ferro instead of guessing a unit name."""
    cfg = _config()
    selected_host = host_for(cfg, project=project, host_id=device)
    selected = resolve_unit(cfg, unit, project=project, host_id=device)
    if selected_host.get("transport") == "local":
        result = _remote(
            "logs",
            {
                "unit": selected,
                "lines": min(1000, max(1, int(lines))),
                "since_minutes": min(10080, max(1, int(since_minutes))),
            },
            project=project,
            device=device,
        )
        _audit("logs", result["returncode"] == 0, {
            "unit": selected,
            "project": project,
            "device": device,
            "lines": lines,
            "transport": "local",
        })
        return result
    result = _ssh([
        "journalctl", "--no-pager", "-u", selected,
        "-n", str(min(1000, max(1, int(lines)))),
        "--since", f"{min(10080, max(1, int(since_minutes)))} minutes ago",
    ], timeout=30, project=project, device=device)
    _audit("logs", result["returncode"] == 0, {"unit": selected, "project": project, "device": device, "lines": lines})
    return result


@server.tool(
    name="list_agents",
    annotations=ToolAnnotations(
        title="List installed agent adapters",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_agents() -> dict[str, Any]:
    """List explicitly installed adapters; an empty list means direct mode remains available."""
    return {"agents": agent_registry.list_agents(), "direct_mode": True, "adapter_load_error": _AGENT_ADAPTER_LOAD_ERROR}


@server.tool(name="start_agent", annotations=ToolAnnotations(title="Start installed agent",readOnlyHint=False,destructiveHint=True,openWorldHint=False))
def start_agent(adapter_id: str, goal: str, project: str, device: str | None = None,
                options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a registered adapter only on the requested authorized project host."""
    selected = host_for(_config(), project=project, host_id=device)
    if not agent_policy.may_launch(selected["id"],project,adapter_id):
        raise PermissionError("agent launch not approved for this device, project and adapter")
    return agent_sessions.start(adapter_id,goal,project=project,device=selected["id"],authorized=True,options=options)


@server.tool(name="get_agent_session", annotations=ToolAnnotations(title="Get agent session",readOnlyHint=True,destructiveHint=False,openWorldHint=False))
def get_agent_session(session_id: str) -> dict[str, Any]:
    """Read a persisted session through its registered adapter."""
    return agent_sessions.status(session_id)


@server.tool(name="send_agent_message", annotations=ToolAnnotations(title="Send agent message",readOnlyHint=False,destructiveHint=False,openWorldHint=False))
def send_agent_message(session_id: str, message: str) -> dict[str, Any]:
    """Send a bounded message if the adapter supports messaging."""
    return agent_sessions.send(session_id,message)


@server.tool(name="decide_agent_permission", annotations=ToolAnnotations(title="Decide Claude tool permission",readOnlyHint=False,destructiveHint=True,openWorldHint=False))
def decide_agent_permission(session_id: str, request_id: str, decision: str,
                            answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Respond to one pending native Claude CLI tool request; not a host exec grant."""
    return agent_sessions.decide(session_id,request_id,decision,answers=answers)


@server.tool(name="cancel_agent", annotations=ToolAnnotations(title="Cancel agent session",readOnlyHint=False,destructiveHint=True,openWorldHint=False))
def cancel_agent(session_id: str) -> dict[str, Any]:
    """Request cancellation when supported by the adapter."""
    return agent_sessions.cancel(session_id)


@server.tool(name="resume_agent", annotations=ToolAnnotations(title="Resume agent session",readOnlyHint=False,destructiveHint=False,openWorldHint=False))
def resume_agent(session_id: str) -> dict[str, Any]:
    """Resume a session only if the installed adapter supports it."""
    return agent_sessions.resume(session_id)


@server.tool(name="watch_agent_events", annotations=ToolAnnotations(title="Read agent session events",readOnlyHint=True,destructiveHint=False,openWorldHint=False))
def watch_agent_events(session_id: str, after: int = 0) -> dict[str, Any]:
    """Read a bounded event batch from an installed adapter."""
    return agent_sessions.events(session_id,after)


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

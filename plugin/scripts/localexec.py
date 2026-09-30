from __future__ import annotations

import getpass
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any

MAX_OUTPUT_BYTES = 262144


def _roots(request: dict[str, Any]) -> list[pathlib.Path]:
    roots = [pathlib.Path(str(value)).expanduser().resolve(strict=True) for value in request["roots"]]
    if not roots:
        raise RuntimeError("local executor requires at least one configured root")
    return roots


def _inside(roots: list[pathlib.Path], value: str, *, exists: bool = True) -> pathlib.Path:
    path = pathlib.Path(str(value)).expanduser()
    if path.exists() or path.is_symlink():
        path = path.resolve(strict=True)
    else:
        path = path.parent.resolve(strict=True) / path.name
    for root in roots:
        try:
            path.relative_to(root)
            return path
        except ValueError:
            continue
    raise PermissionError("path outside configured roots: " + str(path))


def _digest(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _receipt_path(roots: list[pathlib.Path], execution_id: str) -> pathlib.Path:
    value = str(execution_id or "")
    if not value.startswith("dex_") or not value[4:].isalnum() or len(value) > 64:
        raise ValueError("invalid detached execution id")
    directory = roots[0] / ".livingruntime" / "detached-exec"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (value + ".json")


def _save_receipt(path: pathlib.Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def identity() -> dict[str, Any]:
    started = time.monotonic()
    return {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "user": getpass.getuser(),
                "hostname": socket.gethostname(),
                "cwd": os.getcwd(),
            }
        ),
        "stderr": "",
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }


def inventory(request: dict[str, Any]) -> dict[str, Any]:
    disks = []
    for raw in request.get("roots") or []:
        path = pathlib.Path(str(raw))
        try:
            usage = shutil.disk_usage(path)
            disks.append(
                {
                    "path": str(path),
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                }
            )
        except OSError as exc:
            disks.append({"path": str(path), "error": str(exc)[:200]})

    projects = []
    for item in request.get("projects") or []:
        path = pathlib.Path(str(item["path"]))
        projects.append(
            {
                "name": item["name"],
                "path": item["path"],
                "exists": path.exists(),
                "is_directory": path.is_dir(),
                "git_repo": (path / ".git").exists(),
                "units": [],
            }
        )

    return {
        "hostname": socket.gethostname(),
        "cpu_count": os.cpu_count(),
        "load_average": [],
        "uptime_seconds": None,
        "memory": {"total_bytes": None, "available_bytes": None},
        "disks": disks,
        "projects": projects,
        "services": [],
    }


def _worker_command(receipt_path: pathlib.Path, spec_path: pathlib.Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "local-worker", str(receipt_path), str(spec_path)]
    connector = pathlib.Path(__file__).with_name("connector.py")
    return [sys.executable, str(connector), "local-worker", str(receipt_path), str(spec_path)]


def _spawn_worker(receipt_path: pathlib.Path, spec_path: pathlib.Path, cwd: pathlib.Path) -> subprocess.Popen[bytes]:
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(_worker_command(receipt_path, spec_path), **kwargs)


def _terminate_tree(pid: int) -> None:
    if pid <= 1:
        raise ValueError("refusing pid <= 1")
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0 and _pid_alive(pid):
            raise RuntimeError(
                (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
            )
        return
    os.killpg(pid, 15)


def _bounded(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8", errors="replace")


def handle(request: dict[str, Any]) -> dict[str, Any]:
    roots = _roots(request)
    op = str(request["op"])

    if op == "exec":
        cwd = _inside(roots, request["cwd"])
        if not cwd.is_dir():
            raise ValueError("cwd must be a directory")
        if request.get("detached"):
            execution_id = str(request.get("execution_id") or "")
            receipt_path = _receipt_path(roots, execution_id)
            spec_path = receipt_path.with_suffix(".spec.json")
            heartbeat_seconds = max(
                1.0, min(float(request.get("heartbeat_seconds") or 10.0), 60.0)
            )
            stall_seconds = max(
                heartbeat_seconds * 2.0,
                min(float(request.get("stall_seconds") or 300.0), 86400.0),
            )
            spec = {
                "argv": list(request["argv"]),
                "cwd": str(cwd),
                "heartbeat_seconds": heartbeat_seconds,
                "stall_seconds": stall_seconds,
                "tail_bytes": min(int(request.get("tail_bytes") or 32768), 131072),
            }
            spec_path.write_text(json.dumps(spec, sort_keys=True) + "\n", encoding="utf-8")
            now = time.time()
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
            _save_receipt(receipt_path, result)
            worker = _spawn_worker(receipt_path, spec_path, cwd)
            result["worker_pid"] = worker.pid
            result["pid"] = worker.pid
            _save_receipt(receipt_path, result)
            return result

        limit = min(MAX_OUTPUT_BYTES, max(1, int(request.get("max_output") or MAX_OUTPUT_BYTES)))
        try:
            completed = subprocess.run(
                list(request["argv"]),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=float(request["timeout"]),
                check=False,
            )
            return {
                "returncode": completed.returncode,
                "stdout": _bounded(completed.stdout, limit),
                "stderr": _bounded(completed.stderr, limit),
                "cwd": str(cwd),
                "status": "COMPLETED",
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            if isinstance(stdout, str):
                stdout = stdout.encode("utf-8", errors="replace")
            if isinstance(stderr, str):
                stderr = stderr.encode("utf-8", errors="replace")
            return {
                "returncode": None,
                "stdout": _bounded(stdout, limit),
                "stderr": _bounded(stderr, limit),
                "cwd": str(cwd),
                "status": "TIMED_OUT",
                "timed_out": True,
                "timeout_seconds": request["timeout"],
            }

    if op == "exec_receipt":
        execution_id = str(request.get("execution_id") or "")
        target = _receipt_path(roots, execution_id)
        if not target.is_file():
            return {"found": False, "execution_id": execution_id}
        payload = json.loads(target.read_text(encoding="utf-8"))
        now = time.time()
        heartbeat_at = float(payload.get("last_heartbeat_at") or payload.get("created_at") or now)
        progress_at = float(payload.get("last_progress_at") or payload.get("created_at") or now)
        payload["heartbeat_age_seconds"] = round(max(0.0, now - heartbeat_at), 3)
        payload["progress_age_seconds"] = round(max(0.0, now - progress_at), 3)
        worker_pid = int(payload.get("worker_pid") or payload.get("pid") or 0)
        child_pid = int(payload.get("child_pid") or 0)
        payload["worker_alive"] = _pid_alive(worker_pid)
        payload["child_alive"] = _pid_alive(child_pid)
        observed = str(payload.get("status") or "UNKNOWN")
        if not payload.get("terminal"):
            heartbeat_limit = max(
                30.0, float(payload.get("heartbeat_seconds") or 10.0) * 3.0
            )
            if not payload["worker_alive"] and payload["child_alive"]:
                observed = "ORPHANED"
            elif not payload["worker_alive"]:
                observed = "LOST"
            elif payload["heartbeat_age_seconds"] >= heartbeat_limit:
                observed = "HEARTBEAT_STALE"
        payload["observed_status"] = observed
        payload["found"] = True
        return payload

    if op == "exec_cancel":
        execution_id = str(request.get("execution_id") or "")
        target = _receipt_path(roots, execution_id)
        if not target.is_file():
            return {"found": False, "execution_id": execution_id}
        payload = json.loads(target.read_text(encoding="utf-8"))
        if payload.get("terminal"):
            payload["found"] = True
            payload["cancel_requested"] = False
            return payload
        worker_pid = int(payload.get("worker_pid") or payload.get("pid") or 0)
        _terminate_tree(worker_pid)
        payload.update(
            {
                "status": "CANCELLED",
                "terminal": True,
                "finished_at": time.time(),
                "cancel_requested": True,
                "found": True,
            }
        )
        _save_receipt(target, payload)
        return payload

    if op == "read_file":
        path = _inside(roots, request["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        with path.open("rb") as handle:
            handle.seek(int(request["offset"]))
            data = handle.read(int(request["max_bytes"]))
        return {
            "path": str(path),
            "offset": int(request["offset"]),
            "bytes_read": len(data),
            "content": data.decode("utf-8", errors="replace"),
            "sha256": _digest(path),
        }

    if op == "write_file":
        path = _inside(roots, request["path"], exists=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = request.get("expected_sha256")
        if expected is not None:
            if not path.exists():
                raise FileNotFoundError("expected_sha256 supplied but file does not exist")
            if _digest(path) != expected:
                raise RuntimeError("sha256 mismatch")
        with path.open("a" if request["mode"] == "append" else "w", encoding="utf-8") as handle:
            handle.write(request["content"])
        return {
            "path": str(path),
            "mode": request["mode"],
            "bytes": path.stat().st_size,
            "sha256": _digest(path),
        }

    if op == "list_dir":
        path = _inside(roots, request["path"])
        if not path.is_dir():
            raise ValueError("path is not a directory")
        children = sorted(path.iterdir(), key=lambda item: item.name.lower())
        rows = []
        for child in children[: int(request["max_entries"])]:
            rows.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file" if child.is_file() else "other",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return {
            "path": str(path),
            "entries": rows,
            "truncated": len(children) > len(rows),
        }

    if op == "process":
        raise RuntimeError(
            "process inspection is not exposed by the native local transport yet; "
            "filesystem, git, exec, and durable jobs are available"
        )

    if op == "systemd":
        if os.name == "nt":
            raise RuntimeError("systemd is unavailable on native Windows local devices")
        action = str(request.get("action") or "")
        if action not in {"status", "is-active", "start", "stop", "restart"}:
            raise ValueError("unsupported systemd action")
        unit = str(request.get("unit") or "")
        if not unit:
            raise ValueError("systemd unit is required")
        completed = subprocess.run(
            ["systemctl", action, unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": _bounded(completed.stdout, MAX_OUTPUT_BYTES),
            "stderr": _bounded(completed.stderr, MAX_OUTPUT_BYTES),
        }

    if op == "logs":
        if os.name == "nt":
            raise RuntimeError("journal logs are unavailable on native Windows local devices")
        unit = str(request.get("unit") or "")
        if not unit:
            raise ValueError("journal unit is required")
        lines = min(1000, max(1, int(request.get("lines") or 200)))
        since_minutes = min(10080, max(1, int(request.get("since_minutes") or 60)))
        completed = subprocess.run(
            [
                "journalctl", "--no-pager", "-u", unit,
                "-n", str(lines), "--since", f"{since_minutes} minutes ago",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": _bounded(completed.stdout, MAX_OUTPUT_BYTES),
            "stderr": _bounded(completed.stderr, MAX_OUTPUT_BYTES),
        }

    raise ValueError("unknown local operation: " + op)


def detached_worker(receipt_path: str, spec_path: str) -> int:
    receipt = pathlib.Path(receipt_path)
    spec_file = pathlib.Path(spec_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    heartbeat_seconds = max(1.0, min(float(spec.get("heartbeat_seconds") or 10.0), 60.0))
    stall_seconds = max(
        heartbeat_seconds * 2.0, min(float(spec.get("stall_seconds") or 300.0), 86400.0)
    )
    tail_limit = max(1024, min(int(spec.get("tail_bytes") or 32768), 131072))
    stdout_tail = bytearray()
    stderr_tail = bytearray()
    lock = threading.Lock()
    progress = {"last": time.time()}

    def append(target: bytearray, chunk: bytes) -> None:
        with lock:
            target.extend(chunk)
            if len(target) > tail_limit:
                del target[:-tail_limit]
            progress["last"] = time.time()

    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen(
            list(spec["argv"]),
            cwd=str(spec["cwd"]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )
        now = time.time()
        payload.update(
            {
                "status": "RUNNING",
                "terminal": False,
                "worker_pid": os.getpid(),
                "child_pid": child.pid,
                "started_at": now,
                "last_heartbeat_at": now,
                "last_progress_at": now,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
            }
        )
        _save_receipt(receipt, payload)

        def reader(stream: Any, target: bytearray, counter: str) -> None:
            if stream is None:
                return
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                with lock:
                    payload[counter] = int(payload.get(counter) or 0) + len(chunk)
                append(target, chunk)

        out_thread = threading.Thread(
            target=reader, args=(child.stdout, stdout_tail, "stdout_bytes"), daemon=True
        )
        err_thread = threading.Thread(
            target=reader, args=(child.stderr, stderr_tail, "stderr_bytes"), daemon=True
        )
        out_thread.start()
        err_thread.start()

        while child.poll() is None:
            time.sleep(min(1.0, heartbeat_seconds))
            now = time.time()
            with lock:
                last_progress = float(progress["last"])
                payload.update(
                    {
                        "last_heartbeat_at": now,
                        "last_progress_at": last_progress,
                        "progress_age_seconds": round(max(0.0, now - last_progress), 3),
                        "stdout_tail": stdout_tail.decode("utf-8", errors="replace"),
                        "stderr_tail": stderr_tail.decode("utf-8", errors="replace"),
                        "status": "QUIET" if now - last_progress >= stall_seconds else "RUNNING",
                    }
                )
            _save_receipt(receipt, payload)

        out_thread.join(timeout=2)
        err_thread.join(timeout=2)
        returncode = child.wait()
        now = time.time()
        with lock:
            payload.update(
                {
                    "returncode": returncode,
                    "finished_at": now,
                    "last_heartbeat_at": now,
                    "last_progress_at": float(progress["last"]),
                    "progress_age_seconds": round(max(0.0, now - float(progress["last"])), 3),
                    "stdout_tail": stdout_tail.decode("utf-8", errors="replace"),
                    "stderr_tail": stderr_tail.decode("utf-8", errors="replace"),
                    "terminal": True,
                    "status": "SUCCEEDED" if returncode == 0 else "FAILED",
                }
            )
        _save_receipt(receipt, payload)
        return 0 if returncode == 0 else 1
    except Exception as exc:
        now = time.time()
        payload.update(
            {
                "status": "FAILED",
                "terminal": True,
                "finished_at": now,
                "last_heartbeat_at": now,
                "returncode": None if child is None else child.poll(),
                "error": (type(exc).__name__ + ": " + str(exc))[-1000:],
                "stdout_tail": stdout_tail.decode("utf-8", errors="replace"),
                "stderr_tail": stderr_tail.decode("utf-8", errors="replace"),
            }
        )
        _save_receipt(receipt, payload)
        return 1
    finally:
        try:
            spec_file.unlink()
        except OSError:
            pass

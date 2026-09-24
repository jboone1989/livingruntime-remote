from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bridge  # noqa: E402


@unittest.skipIf(os.name == "nt", "remote agent detached execution is POSIX-only")
class DetachedRemoteAgentTests(unittest.TestCase):
    def test_detached_exec_returns_without_waiting_for_child_and_writes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "worker.py"
            marker = root / "started.txt"
            worker.write_text(
                "from pathlib import Path\n"
                "import time\n"
                f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n"
                "print('phase-1', flush=True)\n"
                "time.sleep(0.4)\n"
                "print('phase-2', flush=True)\n",
                encoding="utf-8",
            )
            execution_id = "dex_abc123"
            payload = {
                "op": "exec",
                "roots": [str(root)],
                "cwd": str(root),
                "argv": [sys.executable, str(worker)],
                "timeout": 30,
                "max_output": 4096,
                "detached": True,
                "execution_id": execution_id,
                "worker_code": bridge._DETACHED_EXEC_WORKER,
                "heartbeat_seconds": 1,
                "stall_seconds": 2,
                "tail_bytes": 4096,
            }
            started = time.monotonic()
            proc = subprocess.run(
                [sys.executable, "-c", bridge._REMOTE_AGENT],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertLess(elapsed, 2.0)
            result = json.loads(proc.stdout.decode("utf-8"))
            self.assertEqual(result["status"], "STARTING")
            self.assertTrue(result["detached"])
            self.assertEqual(result["execution_id"], execution_id)
            worker_pid = int(result["worker_pid"])
            try:
                deadline = time.monotonic() + 2.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())

                receipt = root / ".livingruntime" / "detached-exec" / f"{execution_id}.json"
                self.assertTrue(receipt.is_file())
                deadline = time.monotonic() + 4.0
                persisted = {}
                while time.monotonic() < deadline:
                    persisted = json.loads(receipt.read_text(encoding="utf-8"))
                    if persisted.get("terminal"):
                        break
                    time.sleep(0.05)
                self.assertTrue(persisted.get("terminal"))
                self.assertEqual(persisted["status"], "SUCCEEDED")
                self.assertEqual(persisted["returncode"], 0)
                self.assertIn("phase-2", persisted.get("stdout_tail", ""))
                self.assertGreaterEqual(int(persisted.get("stdout_bytes") or 0), 14)
                self.assertGreater(float(persisted.get("last_heartbeat_at") or 0), 0)

                receipt_probe = {
                    "op": "exec_receipt",
                    "roots": [str(root)],
                    "execution_id": execution_id,
                }
                checked = subprocess.run(
                    [sys.executable, "-c", bridge._REMOTE_AGENT],
                    input=json.dumps(receipt_probe).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=3,
                    check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stderr.decode())
                recovered = json.loads(checked.stdout.decode("utf-8"))
                self.assertTrue(recovered["found"])
                self.assertEqual(recovered["observed_status"], "SUCCEEDED")
                self.assertEqual(recovered["status"], "SUCCEEDED")
            finally:
                try:
                    os.kill(worker_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


    def test_supervisor_reports_quiet_then_recovers_before_terminal_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "stall-worker.py"
            worker.write_text(
                "import time\n"
                "print('begin', flush=True)\n"
                "time.sleep(2.3)\n"
                "print('resumed', flush=True)\n"
                "time.sleep(0.2)\n",
                encoding="utf-8",
            )
            execution_id = "dex_stall123"
            payload = {
                "op": "exec",
                "roots": [str(root)],
                "cwd": str(root),
                "argv": [sys.executable, str(worker)],
                "timeout": 30,
                "max_output": 4096,
                "detached": True,
                "execution_id": execution_id,
                "worker_code": bridge._DETACHED_EXEC_WORKER,
                "heartbeat_seconds": 1,
                "stall_seconds": 2,
                "tail_bytes": 4096,
            }
            started = subprocess.run(
                [sys.executable, "-c", bridge._REMOTE_AGENT],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
            )
            self.assertEqual(started.returncode, 0, started.stderr.decode())
            receipt = root / ".livingruntime" / "detached-exec" / f"{execution_id}.json"
            saw_quiet = False
            deadline = time.monotonic() + 5.0
            final = {}
            while time.monotonic() < deadline:
                final = json.loads(receipt.read_text(encoding="utf-8"))
                if final.get("status") == "QUIET":
                    saw_quiet = True
                if final.get("terminal"):
                    break
                time.sleep(0.05)
            self.assertTrue(saw_quiet)
            self.assertEqual(final.get("status"), "SUCCEEDED")
            self.assertIn("resumed", final.get("stdout_tail", ""))
            self.assertGreaterEqual(float(final.get("last_progress_at") or 0), float(final.get("started_at") or 0))



    def test_sync_exec_timeout_returns_structured_state_instead_of_transport_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "slow.py"
            worker.write_text(
                "import time\n"
                "print('started', flush=True)\n"
                "time.sleep(1)\n",
                encoding="utf-8",
            )
            payload = {
                "op": "exec",
                "roots": [str(root)],
                "cwd": str(root),
                "argv": [sys.executable, str(worker)],
                "timeout": 0.1,
                "max_output": 4096,
                "detached": False,
            }
            proc = subprocess.run(
                [sys.executable, "-c", bridge._REMOTE_AGENT],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            result = json.loads(proc.stdout.decode("utf-8"))
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["status"], "TIMED_OUT")
            self.assertIsNone(result["returncode"])
            self.assertIn("started", result["stdout"])

    def test_receipt_distinguishes_orphaned_child_from_fully_lost_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / ".livingruntime" / "detached-exec"
            directory.mkdir(parents=True)
            execution_id = "dex_orphan123"
            receipt = directory / f"{execution_id}.json"
            now = time.time()
            receipt.write_text(
                json.dumps({
                    "execution_id": execution_id,
                    "status": "RUNNING",
                    "terminal": False,
                    "worker_pid": 999999999,
                    "child_pid": os.getpid(),
                    "created_at": now,
                    "last_heartbeat_at": now,
                    "last_progress_at": now,
                    "heartbeat_seconds": 10,
                }),
                encoding="utf-8",
            )
            payload = {
                "op": "exec_receipt",
                "roots": [str(root)],
                "execution_id": execution_id,
            }
            proc = subprocess.run(
                [sys.executable, "-c", bridge._REMOTE_AGENT],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            result = json.loads(proc.stdout.decode("utf-8"))
            self.assertEqual(result["observed_status"], "ORPHANED")
            self.assertFalse(result["worker_alive"])
            self.assertTrue(result["child_alive"])


if __name__ == "__main__":
    unittest.main()

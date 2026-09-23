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
                "time.sleep(30)\n",
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
            self.assertEqual(result["status"], "STARTED")
            self.assertTrue(result["detached"])
            self.assertEqual(result["execution_id"], execution_id)
            child_pid = int(result["pid"])
            try:
                deadline = time.monotonic() + 2.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())

                receipt = root / ".livingruntime" / "detached-exec" / f"{execution_id}.json"
                self.assertTrue(receipt.is_file())
                persisted = json.loads(receipt.read_text(encoding="utf-8"))
                self.assertEqual(persisted["pid"], child_pid)
                self.assertEqual(persisted["status"], "STARTED")

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
                self.assertEqual(recovered["pid"], child_pid)
            finally:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()

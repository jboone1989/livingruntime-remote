from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import localexec  # noqa: E402


class LocalExecutorTests(unittest.TestCase):
    def test_filesystem_and_exec_stay_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            request = {"roots": [str(root)]}

            written = localexec.handle({
                **request,
                "op": "write_file",
                "path": str(root / "hello.txt"),
                "content": "hello",
                "mode": "replace",
                "expected_sha256": None,
            })
            self.assertEqual(written["bytes"], 5)

            read = localexec.handle({
                **request,
                "op": "read_file",
                "path": str(root / "hello.txt"),
                "offset": 0,
                "max_bytes": 1024,
            })
            self.assertEqual(read["content"], "hello")

            listed = localexec.handle({
                **request,
                "op": "list_dir",
                "path": str(root),
                "max_entries": 20,
            })
            self.assertEqual([row["name"] for row in listed["entries"]], ["hello.txt"])

            executed = localexec.handle({
                **request,
                "op": "exec",
                "argv": [sys.executable, "-c", "print('native-local-ok')"],
                "cwd": str(root),
                "timeout": 10,
                "max_output": 4096,
                "detached": False,
            })
            self.assertEqual(executed["returncode"], 0)
            self.assertIn("native-local-ok", executed["stdout"])

    def test_rejects_path_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            with self.assertRaises(PermissionError):
                localexec.handle({
                    "roots": [tmp],
                    "op": "read_file",
                    "path": str(Path(other) / "secret.txt"),
                    "offset": 0,
                    "max_bytes": 1024,
                })

    def test_local_systemd_and_logs_use_bounded_native_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            completed = localexec.subprocess.CompletedProcess(
                ["systemctl"], 0, stdout=b"active\n", stderr=b""
            )
            with patch.object(localexec.subprocess, "run", return_value=completed) as run:
                status = localexec.handle({
                    "roots": [str(root)],
                    "op": "systemd",
                    "action": "is-active",
                    "unit": "demo.service",
                })
                self.assertEqual(status["returncode"], 0)
                self.assertEqual(status["stdout"], "active\n")
                self.assertEqual(
                    run.call_args.args[0],
                    ["systemctl", "is-active", "demo.service"],
                )

                logs = localexec.handle({
                    "roots": [str(root)],
                    "op": "logs",
                    "unit": "demo.service",
                    "lines": 25,
                    "since_minutes": 30,
                })
                self.assertEqual(logs["returncode"], 0)
                self.assertEqual(
                    run.call_args.args[0],
                    [
                        "journalctl", "--no-pager", "-u", "demo.service",
                        "-n", "25", "--since", "30 minutes ago",
                    ],
                )


if __name__ == "__main__":
    unittest.main()

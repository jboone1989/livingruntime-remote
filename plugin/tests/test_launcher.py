from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import launcher  # noqa: E402


class HostWorkerLauncherTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "host worker launcher is POSIX-only")
    def test_optional_host_worker_launcher_uses_node_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            worker = home / ".livingruntime" / "bin" / "host-worker-launcher.mjs"
            worker.parent.mkdir(parents=True)
            worker.write_text("process.exit(0);\n", encoding="utf-8")
            node = home / "bin" / "node"
            node.parent.mkdir()
            node.write_text("", encoding="utf-8")

            completed = subprocess.CompletedProcess([str(node), str(worker)], 0)
            with patch.object(launcher.Path, "home", return_value=home), patch.object(
                launcher.shutil, "which", return_value=str(node)
            ), patch.object(launcher.subprocess, "run", return_value=completed) as run:
                self.assertTrue(launcher.start_host_worker_launcher())

            args, kwargs = run.call_args
            self.assertEqual(args[0], [str(node), str(worker)])
            self.assertNotIn("shell", kwargs)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], 5)
            self.assertFalse(kwargs["check"])

    @unittest.skipIf(os.name == "nt", "host worker launcher is POSIX-only")
    def test_missing_host_worker_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(launcher.Path, "home", return_value=home), patch.object(
                launcher.subprocess, "run"
            ) as run:
                self.assertFalse(launcher.start_host_worker_launcher())
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

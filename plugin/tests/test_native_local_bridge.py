from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bridge  # noqa: E402


class NativeLocalBridgeTests(unittest.TestCase):
    def test_local_transport_bypasses_ssh_and_keeps_root_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "remote.json"
            config.write_text(
                json.dumps(
                    {
                        "default_host": "main",
                        "hosts": {
                            "main": {
                                "transport": "local",
                                "ssh_host": "local",
                                "path_style": "posix",
                                "roots": [str(root)],
                                "units": [],
                            }
                        },
                        "projects": {
                            "workspace": {
                                "host": "main",
                                "path": str(root),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"LIVINGRUNTIME_REMOTE_CONFIG": str(config)}
            ), patch.object(
                bridge, "_ssh", side_effect=AssertionError("local transport used SSH")
            ):
                status = bridge.connection_status()
                self.assertTrue(status["ok"])
                self.assertEqual(status["gateway"]["kind"], "bounded-local")
                self.assertEqual(status["connectivity"]["transport"], "local")
                self.assertTrue(status["connectivity"]["local"])
                self.assertIsNone(status["connectivity"]["ssh"])

                written = bridge.write_file(
                    "hello.txt", "native", project="workspace"
                )
                self.assertTrue(Path(written["path"]).is_file())
                read = bridge.read_file("hello.txt", project="workspace")
                self.assertEqual(read["content"], "native")
                listed = bridge.list_dir(project="workspace")
                self.assertIn("hello.txt", [item["name"] for item in listed["entries"]])

                devices = bridge.list_devices()
                self.assertEqual(devices["devices"][0]["transport"], "local")
                self.assertIn("durable_jobs", devices["devices"][0]["capabilities"])

                with self.assertRaisesRegex(RuntimeError, "process inspection"):
                    bridge.process("list")


if __name__ == "__main__":
    unittest.main()

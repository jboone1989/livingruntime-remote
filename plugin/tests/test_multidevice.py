from __future__ import annotations

import json
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault(
    "LIVINGRUNTIME_REMOTE_CONFIG",
    str(Path(__file__).resolve().parent / "fixtures" / "missing-config.json"),
)

import bridge  # noqa: E402


class MultiDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "remote.json"
        self.config.write_text(
            json.dumps(
                {
                    "default_host": "main",
                    "hosts": {
                        "main": {
                            "ssh_host": "main-alias",
                            "roots": ["/home/ubuntu"],
                            "units": ["content-agent.service"],
                        },
                        "vultr": {
                            "ssh_host": "vultr-alias",
                            "roots": ["/root"],
                            "units": ["livingruntime-remote-relay.service"],
                        },
                    },
                    "projects": {
                        "ferro": {
                            "host": "main",
                            "path": "/home/ubuntu/src/content-agent",
                            "units": ["content-agent.service"],
                        },
                        "remote-public": {
                            "host": "vultr",
                            "path": "/root",
                            "units": ["livingruntime-remote-relay.service"],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.calls: list[dict[str, object]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fake_ssh(
        self,
        remote_argv,
        *,
        stdin=None,
        timeout=30,
        project=None,
        device=None,
    ):
        self.calls.append({"argv": remote_argv, "project": project, "device": device})
        command = " ".join(map(str, remote_argv))
        selected = device or ("vultr" if project == "remote-public" else "main")
        if "getpass" in command:
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "user": "root" if selected == "vultr" else "ubuntu",
                        "hostname": "vultr-host" if selected == "vultr" else "main-host",
                        "cwd": "/root" if selected == "vultr" else "/home/ubuntu",
                    }
                ),
                "stderr": "",
                "duration_ms": 3.0,
            }
        if stdin:
            payload = json.loads(stdin.decode("utf-8"))
            if payload["op"] == "exec":
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "returncode": 0,
                            "stdout": selected + "\n",
                            "stderr": "",
                            "cwd": payload["cwd"],
                        }
                    ),
                    "stderr": "",
                    "duration_ms": 4.0,
                }
        raise AssertionError(f"unexpected SSH call: {remote_argv!r}")

    def test_lists_both_devices_and_routes_explicit_device(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(
            bridge, "_ssh", self.fake_ssh
        ):
            inventory = bridge.list_devices()
            status = bridge.connection_status(device="vultr")
            result = bridge.exec(["ps"], device="vultr")

        self.assertEqual(inventory["default_device"], "main")
        self.assertEqual(
            {row["device_id"] for row in inventory["devices"]},
            {"main", "vultr"},
        )
        self.assertTrue(all(row["online"] for row in inventory["devices"]))
        self.assertEqual(status["remote_host"]["device_id"], "vultr")
        self.assertEqual(status["remote_host"]["hostname"], "vultr-host")
        self.assertEqual(result["stdout"], "vultr\n")
        self.assertEqual(result["cwd"], "/root")

    def test_project_and_device_mismatch_fails_closed(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)):
            with self.assertRaisesRegex(RuntimeError, "bound to host"):
                bridge.read_file("README.md", project="ferro", device="vultr")

    def test_unknown_device_fails_closed(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)):
            with self.assertRaisesRegex(RuntimeError, "unknown host"):
                bridge.connection_status(device="missing")

    def test_device_selector_is_exposed_in_mcp_schemas(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(bridge.server.list_tools())}
        self.assertIn("list_devices", tools)
        for name in {
            "connection_status", "read_file", "write_file", "list_dir", "git",
            "exec", "process", "systemd", "logs", "apply_patch",
        }:
            schema = tools[name].inputSchema or {}
            self.assertIn("device", schema.get("properties", {}), name)


if __name__ == "__main__":
    unittest.main()

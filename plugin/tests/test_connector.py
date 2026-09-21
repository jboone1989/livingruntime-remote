from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import connector  # noqa: E402


class ConnectorTests(unittest.TestCase):
    def test_write_remote_config_creates_workspace_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(connector.Path, "home", return_value=Path(tmp)):
                path = connector.write_remote_config("user@example", "/srv/project")
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["hosts"]["main"]["ssh_host"], "user@example")
                self.assertEqual(payload["hosts"]["main"]["roots"], ["/srv/project"])
                self.assertEqual(payload["projects"]["workspace"]["path"], "/srv/project")

    def test_write_remote_config_rejects_non_posix_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(connector.Path, "home", return_value=Path(tmp)):
                with self.assertRaises(ValueError):
                    connector.write_remote_config("host", "relative/path")

    def test_windows_task_command_runs_connector(self):
        command = connector._windows_task_command([r"C:\Program Files\LivingRuntime\connector.exe"])
        self.assertIn("connector.exe", command)
        self.assertIn("run", command)
        self.assertIn("--daemon-log", command)

    def test_linux_systemd_exec_preserves_spaces(self):
        value = connector._systemd_exec(["/opt/Living Runtime/connector"])
        self.assertIn("'/opt/Living Runtime/connector'", value)
        self.assertTrue(value.endswith("run --daemon-log"))

    def test_launchagent_contains_run_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(connector.Path, "home", return_value=Path(tmp)):
                value = connector._launchagent_plist(["/Applications/LivingRuntime Connector"])
                self.assertIn(connector.MACOS_LABEL, value)
                self.assertIn("<string>run</string>", value)
                self.assertIn("connector.log", value)

    def test_install_reuses_existing_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(connector.Path, "home", return_value=home):
                relay = home / ".livingruntime" / "relay.json"
                relay.parent.mkdir(parents=True)
                relay.write_text(
                    json.dumps({
                        "url": "https://remote.livingruntime.com",
                        "device_id": "device-1",
                        "device_token": "token",
                        "name": "test",
                    }),
                    encoding="utf-8",
                )
                with patch.object(connector, "write_remote_config", return_value=home / "remote.json"):
                    with patch.object(connector, "install_binary", return_value=["connector"]):
                        with patch.object(connector, "install_autostart", return_value="test"):
                            result = connector.install(
                                pair_code=None,
                                host="host",
                                root="/srv/project",
                                relay_url="https://remote.livingruntime.com",
                                name="test",
                                project="workspace",
                                skip_ssh_test=True,
                            )
                self.assertTrue(result["ok"])
                self.assertIsNone(result["device_id"])

    def test_no_args_runs_interactive_setup(self):
        with patch.object(connector, "interactive_setup") as setup:
            connector.main([])
        setup.assert_called_once_with()

    def test_interactive_setup_collects_three_inputs(self):
        with patch("builtins.input", side_effect=["ABCD-EFGH", "ubuntu@example", "/srv/project"]):
            with patch.object(connector, "install", return_value={"autostart": "test"}) as install:
                connector.interactive_setup()
        install.assert_called_once()
        kwargs = install.call_args.kwargs
        self.assertEqual(kwargs["pair_code"], "ABCD-EFGH")
        self.assertEqual(kwargs["host"], "ubuntu@example")
        self.assertEqual(kwargs["root"], "/srv/project")

    def test_status_never_returns_device_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(connector.Path, "home", return_value=home):
                relay = home / ".livingruntime" / "relay.json"
                relay.parent.mkdir(parents=True)
                relay.write_text(
                    json.dumps({
                        "url": "https://remote.livingruntime.com",
                        "device_id": "device-1",
                        "device_token": "super-secret",
                        "name": "test",
                    }),
                    encoding="utf-8",
                )
                result = connector.status()
                self.assertEqual(result["device_id"], "device-1")
                self.assertNotIn("device_token", result)
                self.assertNotIn("super-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

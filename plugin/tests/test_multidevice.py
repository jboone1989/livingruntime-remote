from __future__ import annotations

import json
import asyncio
import os
import subprocess
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
        self.permission_store = Path(self.tmp.name) / "exec-permissions.json"

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
                if payload.get("detached"):
                    return {
                        "returncode": 0,
                        "stdout": json.dumps(
                            {
                                "returncode": None,
                                "stdout": "",
                                "stderr": "",
                                "cwd": payload["cwd"],
                                "detached": True,
                                "status": "STARTED",
                                "pid": 4321,
                            }
                        ),
                        "stderr": "",
                        "duration_ms": 4.0,
                    }
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

    def test_relay_self_restart_is_deferred_and_returns_receipt(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_ssh(
            remote_argv,
            *,
            stdin=None,
            timeout=30,
            project=None,
            device=None,
        ):
            calls.append(
                {
                    "argv": remote_argv,
                    "stdin": stdin,
                    "project": project,
                    "device": device,
                }
            )
            self.assertEqual(remote_argv[0:2], ["python3", "-c"])
            payload = json.loads(stdin.decode("utf-8"))
            self.assertEqual(payload["unit"], "livingruntime-remote-relay.service")
            self.assertEqual(payload["root"], "/root")
            self.assertEqual(payload["delay_seconds"], bridge.DEFERRED_RESTART_DELAY_SECONDS)
            receipt_id = payload["receipt_id"]
            receipt = {
                "receipt_id": receipt_id,
                "action": "restart",
                "unit": payload["unit"],
                "status": "RESTART_SCHEDULED",
                "scheduled_at": 100.0,
                "not_before": 103.0,
                "receipt_path": f"/root/.livingruntime/restart-receipts/{receipt_id}.json",
                "returncode": 0,
            }
            return {
                "returncode": 0,
                "stdout": json.dumps(receipt) + "\n",
                "stderr": "",
                "duration_ms": 5.0,
            }

        with patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(
            bridge, "_ssh", fake_ssh
        ), patch.object(
            bridge, "_audit"
        ):
            result = bridge.systemd("restart", project="remote-public")

        self.assertEqual(len(calls), 1)
        self.assertTrue(result["deferred"])
        self.assertEqual(result["status"], "RESTART_SCHEDULED")
        self.assertEqual(result["unit"], "livingruntime-remote-relay.service")
        self.assertTrue(result["receipt_id"].startswith("restart-"))
        self.assertEqual(result["returncode"], 0)

    def test_non_control_plane_restart_remains_synchronous(self) -> None:
        calls: list[list[str]] = []

        def fake_ssh(
            remote_argv,
            *,
            stdin=None,
            timeout=30,
            project=None,
            device=None,
        ):
            calls.append(remote_argv)
            return {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "duration_ms": 5.0,
            }

        with patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(
            bridge, "_ssh", fake_ssh
        ), patch.object(
            bridge, "_audit"
        ):
            result = bridge.systemd("restart", project="ferro")

        self.assertEqual(
            calls,
            [["sudo", "-n", "systemctl", "restart", "content-agent.service"]],
        )
        self.assertNotIn("deferred", result)
        self.assertEqual(result["returncode"], 0)

    def test_deferred_restart_scheduler_persists_scheduled_receipt(self) -> None:
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        fake_sudo = fake_bin / "sudo"
        fake_sudo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_sudo.chmod(0o755)
        receipt_id = "restart-" + ("a" * 20)
        request = {
            "receipt_id": receipt_id,
            "unit": "livingruntime-remote-relay.service",
            "delay_seconds": 3.0,
            "root": self.tmp.name,
            "worker_code": "raise SystemExit(0)",
        }
        env = dict(os.environ)
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", bridge._DEFERRED_RESTART_SCHEDULER],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "RESTART_SCHEDULED")
        receipt_path = Path(payload["receipt_path"])
        self.assertTrue(receipt_path.is_file())
        persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "RESTART_SCHEDULED")
        self.assertEqual(persisted["receipt_id"], receipt_id)

    def test_dynamic_exec_permission_is_host_scoped_by_default(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", self.fake_ssh):
            first = bridge.exec(["free", "-h"], device="main")
            self.assertTrue(first["approval_required"])
            request = first["request"]
            self.assertEqual(request["host_id"], "main")
            self.assertEqual(request["risk"], "read_only_diagnostic")

            grant = bridge.approve_exec_permission(request["request_id"])
            self.assertEqual(grant["scope"], "host")
            self.assertEqual(grant["grant_mode"], "exact")

            main = bridge.exec(["free", "-h"], device="main")
            vultr = bridge.exec(["free", "-h"], device="vultr")

        self.assertEqual(main["stdout"], "main\n")
        self.assertTrue(vultr["approval_required"])
        self.assertEqual(vultr["request"]["host_id"], "vultr")

    def test_read_only_diagnostic_class_can_span_owned_hosts(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", self.fake_ssh):
            first = bridge.exec(["uptime"], device="main")
            grant = bridge.approve_exec_permission(
                first["request"]["request_id"],
                scope="all_owned_hosts",
                grant_mode="diagnostic_class",
            )
            main = bridge.exec(["free", "-h"], device="main")
            vultr = bridge.exec(["nproc"], device="vultr")

        self.assertEqual(grant["scope"], "all_owned_hosts")
        self.assertEqual(grant["grant_mode"], "diagnostic_class")
        self.assertEqual(main["stdout"], "main\n")
        self.assertEqual(vultr["stdout"], "vultr\n")

    def test_non_diagnostic_cannot_receive_global_grant(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", self.fake_ssh):
            first = bridge.exec(["curl", "https://example.com"], device="main")
            self.assertEqual(first["request"]["risk"], "explicit_host_approval")
            with self.assertRaises(PermissionError):
                bridge.approve_exec_permission(
                    first["request"]["request_id"],
                    scope="all_owned_hosts",
                )

    def test_non_diagnostic_exact_grant_is_bound_to_project_and_cwd(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", self.fake_ssh):
            first = bridge.exec(["curl", "https://example.com"], device="main")
            bridge.approve_exec_permission(first["request"]["request_id"])
            root_result = bridge.exec(["curl", "https://example.com"], device="main")
            project_result = bridge.exec(["curl", "https://example.com"], project="ferro")

        self.assertEqual(root_result["stdout"], "main\n")
        self.assertTrue(project_result["approval_required"])
        self.assertEqual(project_result["request"]["project"], "ferro")

    def test_detached_exec_requires_separate_exact_approval(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", self.fake_ssh):
            foreground = bridge.exec(["curl", "https://example.com"], device="main")
            bridge.approve_exec_permission(foreground["request"]["request_id"])
            allowed_foreground = bridge.exec(["curl", "https://example.com"], device="main")

            detached = bridge.exec(
                ["curl", "https://example.com"],
                device="main",
                detached=True,
            )
            self.assertTrue(detached["approval_required"])
            self.assertTrue(detached["request"]["detached"])
            self.assertEqual(detached["request"]["risk"], "explicit_host_approval")
            self.assertNotEqual(
                foreground["request"]["request_id"],
                detached["request"]["request_id"],
            )

            grant = bridge.approve_exec_permission(detached["request"]["request_id"])
            self.assertTrue(grant["detached"])
            started = bridge.exec(
                ["curl", "https://example.com"],
                device="main",
                detached=True,
            )

        self.assertEqual(allowed_foreground["stdout"], "main\n")
        self.assertTrue(started["detached"])
        self.assertEqual(started["status"], "STARTED")
        self.assertEqual(started["pid"], 4321)

    def test_builtin_node_detached_still_requires_operator_approval(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", self.fake_ssh):
            request = bridge.exec(["node", "worker.js"], device="main", detached=True)

        self.assertTrue(request["approval_required"])
        self.assertTrue(request["request"]["detached"])
        self.assertEqual(request["request"]["risk"], "explicit_host_approval")

    def test_detached_exec_recovers_from_transport_loss_using_receipt(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        execution_id = "dex_deadbeef"

        def flaky_ssh(remote_argv, *, stdin=None, timeout=30, project=None, device=None):
            if not stdin:
                raise AssertionError(f"unexpected SSH call: {remote_argv!r}")
            payload = json.loads(stdin.decode("utf-8"))
            if payload["op"] == "exec":
                self.assertTrue(payload["detached"])
                self.assertEqual(payload["execution_id"], execution_id)
                return {
                    "returncode": 255,
                    "stdout": "",
                    "stderr": "connection closed after remote spawn",
                    "duration_ms": 10.0,
                }
            if payload["op"] == "exec_receipt":
                self.assertEqual(payload["execution_id"], execution_id)
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "found": True,
                            "returncode": None,
                            "stdout": "",
                            "stderr": "",
                            "cwd": "/home/ubuntu",
                            "detached": True,
                            "status": "STARTED",
                            "pid": 9876,
                            "execution_id": execution_id,
                        }
                    ),
                    "stderr": "",
                    "duration_ms": 4.0,
                }
            raise AssertionError(f"unexpected payload: {payload!r}")

        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", flaky_ssh), patch.object(
            bridge, "_detached_execution_id", return_value=execution_id
        ):
            request = bridge.exec(["node", "worker.js"], device="main", detached=True)
            bridge.approve_exec_permission(request["request"]["request_id"])
            result = bridge.exec(["node", "worker.js"], device="main", detached=True)

        self.assertEqual(result["status"], "STARTED")
        self.assertEqual(result["pid"], 9876)
        self.assertTrue(result["recovered_after_transport_error"])
        self.assertEqual(result["execution_id"], execution_id)

    def test_hard_denied_exec_cannot_create_approval_request(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ):
            with self.assertRaisesRegex(PermissionError, "hard-denied"):
                bridge.exec(["bash", "-c", "id"], device="main")
        self.assertFalse(self.permission_store.exists())

    def test_revoked_permission_requires_fresh_approval(self) -> None:
        env = {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(self.permission_store)}
        with patch.dict(os.environ, env), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(bridge, "_ssh", self.fake_ssh):
            first = bridge.exec(["free", "-h"], device="main")
            grant = bridge.approve_exec_permission(first["request"]["request_id"])
            allowed = bridge.exec(["free", "-h"], device="main")
            revoked = bridge.revoke_exec_permission(grant["permission_id"])
            again = bridge.exec(["free", "-h"], device="main")

        self.assertEqual(allowed["stdout"], "main\n")
        self.assertIsNotNone(revoked["revoked_at"])
        self.assertTrue(again["approval_required"])
        self.assertNotEqual(
            first["request"]["request_id"],
            again["request"]["request_id"],
        )

    def test_device_selector_is_exposed_in_mcp_schemas(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(bridge.server.list_tools())}
        self.assertIn("list_devices", tools)
        for name in {
            "connection_status", "read_file", "write_file", "list_dir", "git",
            "exec", "process", "systemd", "logs", "apply_patch",
        }:
            schema = tools[name].inputSchema or {}
            self.assertIn("device", schema.get("properties", {}), name)
        exec_schema = tools["exec"].inputSchema or {}
        self.assertIn("detached", exec_schema.get("properties", {}))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import connector  # noqa: E402
import relay_agent  # noqa: E402


class ConnectorTests(unittest.TestCase):
    def test_write_remote_config_creates_workspace_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(connector.Path, "home", return_value=Path(tmp)):
                path = connector.write_remote_config("user@example", "/srv/project")
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["hosts"]["main"]["ssh_host"], "user@example")
                self.assertEqual(payload["hosts"]["main"]["roots"], ["/srv/project"])
                self.assertEqual(payload["projects"]["workspace"]["path"], "/srv/project")

    def test_write_remote_config_supports_native_windows_local_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(connector.Path, "home", return_value=Path(tmp)):
                path = connector.write_remote_config(
                    "local", "D:\\", local=True
                )
                payload = json.loads(path.read_text(encoding="utf-8"))
                host = payload["hosts"]["main"]
                self.assertEqual(host["transport"], "local")
                self.assertEqual(host["ssh_host"], "local")
                self.assertEqual(host["path_style"], "windows")
                self.assertEqual(host["roots"], ["D:\\"])
                self.assertEqual(payload["projects"]["workspace"]["path"], "D:\\")

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

    def test_linux_linger_accepts_existing_persistence(self):
        completed = connector.subprocess.CompletedProcess(
            ["loginctl"], 0, stdout="yes\n", stderr=""
        )
        with patch.object(connector.getpass, "getuser", return_value="ubuntu"), patch.object(
            connector, "_run", return_value=completed
        ) as run:
            connector.ensure_linux_linger()
        run.assert_called_once_with(
            ["loginctl", "show-user", "ubuntu", "-p", "Linger", "--value"],
            check=False,
        )

    def test_linux_linger_enables_persistence_when_possible(self):
        responses = [
            connector.subprocess.CompletedProcess(
                ["loginctl"], 0, stdout="no\n", stderr=""
            ),
            connector.subprocess.CompletedProcess(
                ["loginctl"], 0, stdout="", stderr=""
            ),
            connector.subprocess.CompletedProcess(
                ["loginctl"], 0, stdout="yes\n", stderr=""
            ),
        ]
        with patch.object(connector.getpass, "getuser", return_value="root"), patch.object(
            connector, "_run", side_effect=responses
        ) as run:
            connector.ensure_linux_linger()
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["loginctl", "enable-linger", "root"],
        )

    def test_linux_linger_fails_loudly_when_persistence_cannot_be_enabled(self):
        responses = [
            connector.subprocess.CompletedProcess(
                ["loginctl"], 0, stdout="no\n", stderr=""
            ),
            connector.subprocess.CompletedProcess(
                ["loginctl"], 1, stdout="", stderr="permission denied"
            ),
        ]
        with patch.object(connector.getpass, "getuser", return_value="ubuntu"), patch.object(
            connector, "_run", side_effect=responses
        ):
            with self.assertRaisesRegex(RuntimeError, "enable-linger ubuntu"):
                connector.ensure_linux_linger()

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

    def test_relay_agent_dispatches_claimed_tasks_concurrently(self):
        cfg = {
            "url": "https://remote.example",
            "device_token": "token",
        }
        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        overlapped = {"value": False}
        results: list[str] = []
        poll_count = {"value": 0}
        lock = threading.Lock()

        def fake_dispatch(task):
            task_id = task["task_id"]
            if task_id == "task-1":
                first_started.set()
                self.assertTrue(release_first.wait(2))
            elif task_id == "task-2":
                overlapped["value"] = first_started.is_set() and not release_first.is_set()
                second_started.set()
                release_first.set()
            return {"ok": True, "result": {"task_id": task_id}}

        def fake_request(base, path, body, token=None, timeout=35):
            if path.startswith("/device/poll"):
                with lock:
                    poll_count["value"] += 1
                    index = poll_count["value"]
                    done = len(results)
                if index == 1:
                    return {"task": {"task_id": "task-1", "tool": "diagnostics", "args": {}}}
                if index == 2:
                    self.assertTrue(first_started.wait(2))
                    return {"task": {"task_id": "task-2", "tool": "diagnostics", "args": {}}}
                if done >= 2:
                    raise KeyboardInterrupt
                return {"task": None}
            if path == "/device/result":
                with lock:
                    results.append(body["task_id"])
                return {}
            raise AssertionError(path)

        with patch.object(relay_agent, "_load", return_value=cfg), patch.object(
            relay_agent, "_dispatch", side_effect=fake_dispatch
        ), patch.object(relay_agent, "_request", side_effect=fake_request):
            relay_agent.serve(Path("/unused"))

        self.assertTrue(second_started.is_set())
        self.assertTrue(overlapped["value"])
        self.assertEqual(set(results), {"task-1", "task-2"})

    def test_relay_agent_forwards_terminal_job_event_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "jobs"
            with patch.dict("os.environ", {"LIVINGRUNTIME_REMOTE_JOBS": str(root)}):
                created = relay_agent.jobs.create(goal="event", project="ferro", device="main")
                completed = relay_agent.jobs.checkpoint(
                    created["job_id"],
                    summary="done",
                    status="SUCCEEDED",
                )
                event_id = completed["completion_event"]["event_id"]
                calls = []

                def fake_request(base, path, body, token=None, timeout=35):
                    calls.append((path, body))
                    return {"ok": True, "matching_subscriptions": 1, "delivered": 1}

                cfg = {
                    "url": "https://remote.example",
                    "device_token": "token",
                }
                with patch.object(relay_agent, "_request", side_effect=fake_request):
                    relay_agent._forward_completion_events(cfg)
                    relay_agent._forward_completion_events(cfg)

                self.assertEqual([path for path, _ in calls], ["/device/event"])
                self.assertEqual(calls[0][1]["eventId"], event_id)
                self.assertEqual(calls[0][1]["name"], "job.completed")
                self.assertEqual(calls[0][1]["data"]["project"], "ferro")

    def test_relay_agent_forwards_cognition_event_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cognition"
            with patch.dict(
                "os.environ",
                {
                    "LIVINGRUNTIME_COGNITION_ROOT": str(root),
                },
            ):
                created = relay_agent.cognition.submit(
                    agent_id="ferro",
                    purpose="self_repair",
                    messages=[{"role": "user", "content": "private cognition prompt"}],
                    metadata={"routing_task_class": "self_repair"},
                    request_id="llmreq_event_test",
                )
                calls = []

                def fake_request(base, path, body, token=None, timeout=35):
                    calls.append((path, body))
                    return {"ok": True, "matching_subscriptions": 1, "delivered": 1}

                cfg = {
                    "url": "https://remote.example",
                    "device_token": "token",
                }
                with patch.object(relay_agent, "_request", side_effect=fake_request):
                    relay_agent._forward_cognition_events(cfg)
                    relay_agent._forward_cognition_events(cfg)

                self.assertEqual([path for path, _ in calls], ["/device/event"])
                payload = calls[0][1]
                self.assertEqual(payload["eventId"], created["activation_event"]["event_id"])
                self.assertEqual(payload["name"], "cognition.requested")
                self.assertEqual(payload["data"]["request_id"], "llmreq_event_test")
                self.assertEqual(payload["data"]["agent_id"], "ferro")
                self.assertEqual(payload["data"]["lane"], "repair")
                self.assertNotIn("messages", payload)
                self.assertNotIn("private cognition prompt", json.dumps(payload))

    def test_relay_agent_does_not_consume_cognition_event_without_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cognition"
            with patch.dict(
                "os.environ",
                {"LIVINGRUNTIME_COGNITION_ROOT": str(root)},
            ):
                created = relay_agent.cognition.submit(
                    agent_id="ferro",
                    purpose="owner_dialogue",
                    messages=[{"role": "user", "content": "wake me"}],
                    request_id="llmreq_no_subscription",
                )
                cfg = {
                    "url": "https://remote.example",
                    "device_token": "token",
                }
                with patch.object(
                    relay_agent,
                    "_request",
                    return_value={
                        "ok": True,
                        "matching_subscriptions": 0,
                        "delivered": 0,
                    },
                ):
                    relay_agent._forward_cognition_events(cfg)
                stored = relay_agent.cognition.get(created["request_id"])
                self.assertIsNone(stored["activation_event"]["event_forwarded_at"])



if __name__ == "__main__":
    unittest.main()

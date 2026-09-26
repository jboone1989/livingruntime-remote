from __future__ import annotations

import asyncio
import json
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

from contract import HANDSHAKE_TOOLS, PLUGIN_VERSION, REMOTE_IDENTITY, REMOTE_TOOLS, schema_hash  # noqa: E402
import bridge  # noqa: E402
import credentials  # noqa: E402
import jobs  # noqa: E402


def _write_config(directory: Path) -> Path:
    path = directory / "remote.json"
    path.write_text(
        json.dumps(
            {
                "default_host": "main",
                "hosts": {
                    "main": {
                        "ssh_host": "livingruntime-vm",
                        "roots": ["/home/ubuntu"],
                        "units": [],
                    }
                },
                "projects": {
                    "virtualbrain": {
                        "host": "main",
                        "path": "/home/ubuntu/virtualbrain/current",
                    },
                    "ferro": {
                        "host": "main",
                        "path": "/home/ubuntu/wechat-traffic-agent",
                        "units": ["content-agent.service"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class DiscoveryTests(unittest.TestCase):
    def test_tools_list_matches_remote_tools_exactly(self) -> None:
        names = bridge.advertised_tool_names()
        self.assertEqual(names, sorted(REMOTE_TOOLS), msg=f"tools/list drifted: {names}")

    def test_async_list_tools_matches_remote_tools(self) -> None:
        tools = asyncio.run(bridge.server.list_tools())
        names = sorted(tool.name for tool in tools)
        self.assertEqual(names, sorted(REMOTE_TOOLS))
        by_name = {tool.name: tool for tool in tools}
        for name in REMOTE_TOOLS:
            schema = by_name[name].inputSchema or {}
            self.assertEqual(schema.get("type"), "object")
            self.assertTrue(by_name[name].description)

    def test_complete_llm_request_accepts_structured_response_text(self) -> None:
        tools = asyncio.run(bridge.server.list_tools())
        complete = next(tool for tool in tools if tool.name == "complete_llm_request")
        schema = (complete.inputSchema or {}).get("properties", {}).get("response_text", {})
        variants = schema.get("anyOf") or []
        types = {item.get("type") for item in variants if isinstance(item, dict)}
        self.assertIn("string", types)
        self.assertIn("object", types)
        self.assertIn("array", types)
        self.assertEqual(
            bridge._normalize_cognition_response_text({"answer": "ok"}),
            '{"answer":"ok"}',
        )

    def test_capabilities_handshake_shape(self) -> None:
        snapshot = bridge.capabilities()
        self.assertEqual(snapshot["identity"], REMOTE_IDENTITY)
        self.assertEqual(snapshot["version"], PLUGIN_VERSION)
        self.assertEqual(snapshot["schema_hash"], schema_hash())
        self.assertEqual(snapshot["tools"], list(HANDSHAKE_TOOLS))
        self.assertEqual(snapshot["available"], list(REMOTE_TOOLS))
        self.assertEqual(snapshot["server_tools"], list(REMOTE_TOOLS))
        self.assertEqual(snapshot["health_scope"], "mcp_tools_list")
        self.assertEqual(snapshot["missing"], [])
        self.assertTrue(snapshot["healthy"])
        self.assertIn("diagnostics", snapshot["available"])

    def test_diagnostics_reports_process_registry(self) -> None:
        report = bridge.diagnostics()
        self.assertEqual(report["remote_id"], REMOTE_IDENTITY)
        self.assertEqual(report["version"], PLUGIN_VERSION)
        self.assertTrue(report["session_attach"])
        self.assertEqual(report["schema_hash"], schema_hash())
        self.assertIsInstance(report["last_tool_refresh"], float)
        self.assertEqual(report["missing"], [])

    def test_core_tool_annotations_are_explicit(self) -> None:
        tools = asyncio.run(bridge.server.list_tools())
        by_name = {tool.name: tool for tool in tools}
        expected = {
            "capabilities": (True, False, False),
            "diagnostics": (True, False, False),
            "remote_overview": (True, False, False),
            "list_projects": (True, False, False),
            "read_file": (True, False, False),
            "apply_patch": (False, True, False),
            "git": (False, True, True),
            "logs": (True, False, False),
            "list_credentials": (True, False, False),
            "lease_credential": (False, False, False),
            "list_credential_leases": (True, False, False),
            "revoke_credential_lease": (False, True, False),
            "github_identity": (False, False, True),
            "create_job": (False, False, False),
            "get_job": (True, False, False),
            "list_jobs": (True, False, False),
            "checkpoint_job": (False, False, False),
            "submit_llm_request": (False, False, False),
            "watch_agent_cognition": (True, False, False),
            "wait_llm_request": (True, False, False),
            "claim_llm_request": (False, False, False),
            "get_llm_request": (True, False, False),
            "get_llm_request_status": (True, False, False),
            "complete_llm_request": (False, False, False),
            "start_long_job": (False, True, True),
            "watch_long_job": (True, False, False),
            "get_long_job": (True, False, False),
            "wait_long_job": (True, False, False),
            "cancel_long_job": (False, True, False),
            "start_pi_agent": (False, True, False),
            "start_pi_step": (False, True, False),
            "watch_pi_job": (True, False, False),
            "wait_pi_job_completion": (True, False, False),
            "bind_openai_pi_continuation": (False, False, False),
            "continue_openai_pi_job": (False, False, False),
        }
        for name, (read_only, destructive, open_world) in expected.items():
            annotations = by_name[name].annotations
            self.assertIsNotNone(annotations)
            self.assertEqual(annotations.readOnlyHint, read_only, name)
            self.assertEqual(annotations.destructiveHint, destructive, name)
            self.assertEqual(annotations.openWorldHint, open_world, name)


class SchemaAndToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _write_config(Path(self.tmp.name))
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("hello from livingruntime\n", encoding="utf-8")
        self.audit = Path(self.tmp.name) / "remote-audit.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ssh(self, remote_argv, *, stdin=None, timeout=30, project=None, device=None):
        command = " ".join(map(str, remote_argv))
        if "getpass" in command:
            return {
                "returncode": 0,
                "stdout": json.dumps({"user": "ubuntu", "hostname": "livingruntime-host", "cwd": "/home/ubuntu"}),
                "stderr": "",
                "duration_ms": 12.5,
            }
        if remote_argv and remote_argv[0] == "journalctl":
            self.assertIn("content-agent.service", remote_argv)
            return {
                "returncode": 0,
                "stdout": "Sep 20 00:00:00 host content-agent[1]: ready\n",
                "stderr": "",
                "duration_ms": 8.0,
            }
        if stdin:
            payload = json.loads(stdin.decode("utf-8"))
            if payload["op"] == "read_file":
                content = (self.workspace / "README.md").read_text(encoding="utf-8")
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "path": payload["path"],
                            "offset": 0,
                            "bytes_read": len(content),
                            "content": content,
                            "sha256": "deadbeef",
                        }
                    ),
                    "stderr": "",
                    "duration_ms": 9.0,
                }
            if payload["op"] == "exec" and payload.get("argv", [None])[0] == "git":
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "returncode": 0,
                            "stdout": "On branch main\n",
                            "stderr": "",
                            "cwd": payload.get("cwd"),
                        }
                    ),
                    "stderr": "",
                    "duration_ms": 11.0,
                }
        raise AssertionError(f"unexpected ssh call {remote_argv!r}")

    def test_connection_status_shape_and_no_secrets(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(bridge, "_ssh", self._ssh):
            status = bridge.connection_status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["endpoint"]["reachable"])
        self.assertEqual(status["remote_host"]["hostname"], "livingruntime-host")
        self.assertTrue(status["connectivity"]["ssh"])
        self.assertTrue(status["connectivity"]["authenticated"])
        self.assertTrue(status["connectivity"]["authorized"])
        self.assertIsNotNone(status["connectivity"]["latency_ms"])
        self.assertIsNotNone(status["connectivity"]["last_success_unix"])
        blob = json.dumps(status)
        self.assertNotIn("sk-", blob)
        self.assertNotIn("TOKEN", blob)
        self.assertNotIn("BEGIN", blob)
        self.assertEqual(status["core_tools"], list(REMOTE_TOOLS))
        self.assertTrue(status["remote_reachable"])
        self.assertTrue(status["toolset_loaded"])
        self.assertIsNone(status["reason"])
        self.assertTrue(status["capabilities"]["healthy"])
        self.assertEqual(status["capabilities"]["identity"], REMOTE_IDENTITY)
        self.assertEqual(status["capabilities"]["missing"], [])

    def test_read_file_git_logs_integration(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(bridge, "_ssh", self._ssh):
            file_result = bridge.read_file("/home/ubuntu/README.md")
            git_result = bridge.git(["status"], repo_path="/home/ubuntu")
            log_result = bridge.logs(unit="content-agent.service", lines=50)
        self.assertIn("hello from livingruntime", file_result["content"])
        self.assertEqual(git_result["returncode"], 0)
        self.assertIn("content-agent", log_result["stdout"])

    def test_project_aliases_resolve_without_absolute_paths(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(bridge, "_ssh", self._ssh):
            catalog = bridge.list_projects()
            file_result = bridge.read_file("README.md", project="virtualbrain")
            git_result = bridge.git(["status"], project="virtualbrain")
            log_result = bridge.logs(project="ferro", lines=100)
        names = {row["name"] for row in catalog["projects"]}
        self.assertEqual(names, {"virtualbrain", "ferro"})
        self.assertEqual(file_result["path"], "/home/ubuntu/virtualbrain/current/README.md")
        self.assertEqual(git_result["cwd"], "/home/ubuntu/virtualbrain/current")
        self.assertIn("content-agent", log_result["stdout"])

    def test_logs_reject_units_outside_allowlist(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)):
            with self.assertRaises(PermissionError):
                bridge.logs(unit="ssh.service")

    def test_git_blocks_credential_helper(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)):
            with self.assertRaises(PermissionError):
                bridge.git(args=["-c", "credential.helper=!evil", "status"], repo_path="/home/ubuntu")

    def test_http_transport_refuses_public_bind(self) -> None:
        with self.assertRaises(ValueError):
            bridge.configure_http_transport("0.0.0.0", 8766)
        bridge.configure_http_transport("127.0.0.1", 8766)
        self.assertEqual(bridge._ACTIVE_TRANSPORT, "streamable-http")
        self.assertEqual(bridge._HTTP_BIND, "127.0.0.1:8766")
        self.assertFalse(bridge._transport_endpoint()["public_ingress"])

    def test_durable_job_survives_model_turn_boundaries(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(bridge, "_config_path", return_value=str(self.config)):
            created = bridge.create_job(
                "Repair Ferro and verify tests",
                project="ferro",
            )
            checkpointed = bridge.checkpoint_job(
                created["job_id"],
                "Logs inspected; patch is next.",
                current_step="inspect logs",
                next_action="apply patch",
                status="WAITING",
            )
            recovered = bridge.get_job(created["job_id"])
            listed = bridge.list_jobs(status="WAITING")

        self.assertEqual(checkpointed["status"], "WAITING")
        self.assertEqual(recovered["next_action"], "apply patch")
        self.assertEqual(listed["jobs"][0]["job_id"], created["job_id"])

    def test_list_jobs_reconciles_terminal_exec_before_status_filter(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(bridge, "_audit"):
            created = jobs.create(
                goal="Detached command",
                project="ferro",
                device="main",
                backend={
                    "type": "exec",
                    "job_id": "dex_done",
                    "cwd": "/home/ubuntu/wechat-traffic-agent",
                    "executable": "python3",
                },
                status="PENDING",
            )
            jobs.update_runtime(
                created["job_id"],
                runtime={
                    "backend_status": "RUNNING",
                    "observed_status": "RUNNING",
                    "worker_alive": True,
                    "child_alive": False,
                },
                status="RUNNING",
            )
            receipt = {
                "found": True,
                "status": "SUCCEEDED",
                "observed_status": "SUCCEEDED",
                "terminal": True,
                "returncode": 0,
                "worker_alive": False,
                "child_alive": False,
                "finished_at": 123.0,
                "stdout_bytes": 12,
                "stderr_bytes": 0,
            }
            with patch.object(bridge, "_long_job_receipt", return_value=receipt):
                running = bridge.list_jobs(status="RUNNING")
                succeeded = bridge.list_jobs(status="SUCCEEDED")

            persisted = jobs.get(created["job_id"])

        self.assertEqual(running["jobs"], [])
        self.assertEqual([row["job_id"] for row in succeeded["jobs"]], [created["job_id"]])
        self.assertEqual(persisted["status"], "SUCCEEDED")
        self.assertTrue(persisted["terminal"])
        self.assertEqual(persisted["runtime"]["returncode"], 0)
        self.assertEqual(persisted["checkpoints"][-1]["source"], "backend")

    def test_get_job_reconciles_terminal_exec_without_watcher(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(bridge, "_audit"):
            created = jobs.create(
                goal="Detached command",
                device="main",
                backend={
                    "type": "exec",
                    "job_id": "dex_failed",
                    "cwd": "/home/ubuntu",
                    "executable": "python3",
                },
                status="PENDING",
            )
            jobs.update_runtime(
                created["job_id"],
                runtime={
                    "backend_status": "RUNNING",
                    "observed_status": "RUNNING",
                    "worker_alive": True,
                    "child_alive": False,
                },
                status="RUNNING",
            )
            with patch.object(
                bridge,
                "_long_job_receipt",
                return_value={
                    "found": True,
                    "status": "FAILED",
                    "observed_status": "FAILED",
                    "terminal": True,
                    "returncode": 7,
                    "worker_alive": False,
                    "child_alive": False,
                    "finished_at": 456.0,
                    "stdout_bytes": 0,
                    "stderr_bytes": 44,
                },
            ):
                refreshed = bridge.get_job(created["job_id"])

        self.assertEqual(refreshed["status"], "FAILED")
        self.assertTrue(refreshed["terminal"])
        self.assertEqual(refreshed["runtime"]["returncode"], 7)

    def test_job_reconcile_is_fail_soft_when_receipt_is_temporarily_unavailable(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(bridge, "_audit"):
            created = jobs.create(
                goal="Detached command",
                device="main",
                backend={
                    "type": "exec",
                    "job_id": "dex_unreachable",
                    "cwd": "/home/ubuntu",
                    "executable": "python3",
                },
                status="PENDING",
            )
            jobs.update_runtime(
                created["job_id"],
                runtime={
                    "backend_status": "RUNNING",
                    "observed_status": "RUNNING",
                    "worker_alive": True,
                    "child_alive": False,
                },
                status="RUNNING",
            )
            with patch.object(
                bridge,
                "_long_job_receipt",
                side_effect=RuntimeError("host temporarily unavailable"),
            ):
                listed = bridge.list_jobs(status="RUNNING")

        self.assertEqual([row["job_id"] for row in listed["jobs"]], [created["job_id"]])
        self.assertIn("temporarily unavailable", listed["jobs"][0]["runtime"]["reconcile_error"])

    def test_pi_running_requires_process_alive_receipt(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        with patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)}):
            goal = jobs.create(goal="Pi liveness truth", project="ferro", device="main")
            goal = jobs.attach_backend(
                goal["job_id"],
                {
                    "type": "pi-agent",
                    "job_id": "pi-liveness",
                    "pi_remote_dir": "/home/ubuntu/src/pi-remote-runtime",
                },
            )
            stale = bridge._sync_pi_runtime_job(
                goal,
                {"status": "RUNNING", "processAlive": False},
            )
            self.assertEqual(stale["status"], "PENDING")
            self.assertFalse(stale["runtime"]["worker_alive"])

            live = bridge._sync_pi_runtime_job(
                stale,
                {"status": "RUNNING", "processAlive": True},
            )
            self.assertEqual(live["status"], "RUNNING")
            self.assertTrue(live["runtime"]["worker_alive"])

    def test_start_pi_agent_uses_native_api_session_and_chatgpt_provider(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        session_root = Path(self.tmp.name) / "pi-sessions"
        job_root = Path(self.tmp.name) / "pi-jobs"
        session_file = session_root / "session.jsonl"
        calls = []

        def fake_pi(command, payload, timeout_seconds=30):
            calls.append((command, payload))
            if command == "create":
                session_root.mkdir(parents=True, exist_ok=True)
                session_file.write_text("{}\n", encoding="utf-8")
                return {
                    "sessionId": "session-agent-1",
                    "sessionFile": str(session_file),
                    "cwd": "/home/ubuntu/wechat-traffic-agent",
                    "controllerMode": "api",
                    "piLlmCalls": 0,
                }
            if command == "job-start":
                return {
                    "job": {
                        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "status": "QUEUED",
                        "kind": "prompt-api",
                    },
                    "launch": {"detached": True, "pid": 22345},
                }
            raise AssertionError(command)

        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(
            bridge,
            "_pi_runtime_settings",
            return_value=("main", "/home/ubuntu/src/pi-remote-runtime", str(session_root), str(job_root)),
        ), patch.object(
            bridge, "_pi_control_command", side_effect=fake_pi
        ), patch.object(
            bridge, "_audit"
        ):
            result = bridge.start_pi_agent("Inspect README and report.", "ferro")
            durable = bridge.get_job(result["runtimeJobId"])

        self.assertEqual(result["controllerMode"], "api")
        self.assertEqual(result["provider"], "livingruntime-chatgpt")
        self.assertEqual(result["model"], "chatgpt-web")
        self.assertEqual(result["jobId"], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.assertEqual(durable["backend"]["type"], "pi-agent")
        self.assertEqual(durable["backend"]["controller_mode"], "api")
        self.assertEqual(durable["backend"]["provider"], "livingruntime-chatgpt")
        self.assertEqual([call[0] for call in calls], ["create", "job-start"])
        launch_payload = calls[1][1]
        self.assertEqual(launch_payload["kind"], "prompt-api")
        self.assertEqual(launch_payload["provider"], "livingruntime-chatgpt")
        self.assertEqual(launch_payload["modelId"], "chatgpt-web")
        self.assertEqual(launch_payload["text"], "Inspect README and report.")
        self.assertEqual(launch_payload["credentialBindings"], {})

    def test_pi_agent_completion_finishes_the_durable_goal(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        with patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)}), patch.object(bridge, "_audit"):
            goal = jobs.create(goal="Native Pi goal", project="ferro", device="main")
            jobs.attach_backend(
                goal["job_id"],
                {"type": "pi-agent", "job_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
            )
            with patch.object(
                bridge,
                "_pi_job_command",
                return_value={"terminal": True, "timedOut": False, "state": {"status": "SUCCEEDED"}},
            ):
                waited = bridge.wait_pi_job_completion("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", timeout_seconds=5)
            durable = bridge.get_job(goal["job_id"])
        self.assertEqual(waited["runtimeGoalStatus"], "SUCCEEDED")
        self.assertTrue(waited["runtimeGoalTerminal"])
        self.assertEqual(durable["status"], "SUCCEEDED")

    def test_start_pi_step_creates_external_session_and_durable_backend_job(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        session_root = Path(self.tmp.name) / "pi-sessions"
        job_root = Path(self.tmp.name) / "pi-jobs"
        session_file = session_root / "session.jsonl"
        calls = []

        def fake_pi(command, payload, timeout_seconds=30):
            calls.append((command, payload))
            if command == "create":
                session_root.mkdir(parents=True, exist_ok=True)
                session_file.write_text("{}\n", encoding="utf-8")
                return {
                    "sessionId": "session-1",
                    "sessionFile": str(session_file),
                    "cwd": "/home/ubuntu/wechat-traffic-agent",
                    "controllerMode": "external",
                    "piLlmCalls": 0,
                }
            if command == "job-start":
                return {
                    "job": {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "status": "QUEUED",
                        "kind": "external-actions",
                    },
                    "launch": {"detached": True, "pid": 12345},
                }
            raise AssertionError(command)

        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(
            bridge,
            "_pi_runtime_settings",
            return_value=("main", "/home/ubuntu/src/pi-remote-runtime", str(session_root), str(job_root)),
        ), patch.object(
            bridge, "_pi_control_command", side_effect=fake_pi
        ), patch.object(
            bridge, "_audit"
        ):
            result = bridge.start_pi_step(
                "Inspect one file",
                "ferro",
                [{"role": "planner", "tool": "read", "params": {"path": "README.md"}}],
            )
            self.assertEqual(result["controllerMode"], "external")
            self.assertEqual(result["piLlmCallsExpectedDelta"], 0)
            self.assertEqual(result["jobId"], "11111111-1111-4111-8111-111111111111")
            self.assertTrue(result["runtimeJobId"].startswith("lrjob_"))
            durable = bridge.get_job(result["runtimeJobId"])
            self.assertEqual(durable["status"], "PENDING")
            self.assertFalse(durable["runtime"]["worker_alive"])
            self.assertEqual(durable["backend"]["type"], "pi-step")
            self.assertEqual(durable["backend"]["job_id"], result["jobId"])
            self.assertEqual(durable["backend"]["session_file"], str(session_file))
            self.assertEqual(durable["backend"]["session_id"], "session-1")
            self.assertEqual([call[0] for call in calls], ["create", "job-start"])

    def test_pi_step_completion_waits_for_controller_instead_of_finishing_goal(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(bridge, "_audit"):
            goal = jobs.create(goal="Multi-step goal", project="ferro", device="main")
            jobs.attach_backend(
                goal["job_id"],
                {
                    "type": "pi-step",
                    "job_id": "22222222-2222-4222-8222-222222222222",
                    "pi_remote_dir": "/home/ubuntu/src/pi-remote-runtime",
                    "job_root": "/home/ubuntu/.livingruntime/pi-jobs",
                    "session_file": "/home/ubuntu/.livingruntime/pi-sessions/session.jsonl",
                    "session_id": "session-2",
                    "controller_mode": "external",
                },
            )
            with patch.object(
                bridge,
                "_pi_job_command",
                return_value={
                    "terminal": True,
                    "timedOut": False,
                    "state": {
                        "status": "SUCCEEDED",
                        "piLlmCallsDelta": 0,
                    },
                },
            ):
                waited = bridge.wait_pi_job_completion(
                    "22222222-2222-4222-8222-222222222222",
                    "/home/ubuntu/src/pi-remote-runtime",
                    "/home/ubuntu/.livingruntime/pi-jobs",
                    timeout_seconds=5,
                )
            durable = bridge.get_job(goal["job_id"])

        self.assertEqual(waited["runtimeJobId"], goal["job_id"])
        self.assertEqual(waited["runtimeGoalStatus"], "WAITING")
        self.assertFalse(waited["runtimeGoalTerminal"])
        self.assertEqual(durable["status"], "WAITING")
        self.assertFalse(durable["terminal"])
        self.assertIn("next bounded step", durable["next_action"])

    def test_start_pi_step_continues_same_goal_and_reuses_external_session(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        session_root = Path(self.tmp.name) / "pi-sessions"
        job_root = Path(self.tmp.name) / "pi-jobs"
        session_root.mkdir(parents=True)
        session_file = session_root / "session.jsonl"
        session_file.write_text("{}\n", encoding="utf-8")
        calls = []
        job_ids = iter([
            "33333333-3333-4333-8333-333333333333",
            "44444444-4444-4444-8444-444444444444",
        ])

        def fake_pi(command, payload, timeout_seconds=30):
            calls.append((command, payload))
            if command == "create":
                return {
                    "sessionId": "session-reuse",
                    "sessionFile": str(session_file),
                    "cwd": "/home/ubuntu/wechat-traffic-agent",
                    "controllerMode": "external",
                    "piLlmCalls": 0,
                }
            if command == "state":
                return {
                    "sessionId": "session-reuse",
                    "sessionFile": str(session_file),
                    "cwd": "/home/ubuntu/wechat-traffic-agent",
                    "controllerMode": "external",
                    "piLlmCalls": 0,
                }
            if command == "job-start":
                job_id = next(job_ids)
                return {
                    "job": {"id": job_id, "status": "QUEUED", "kind": "external-actions"},
                    "launch": {"detached": True, "pid": 12345},
                }
            raise AssertionError(command)

        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root)},
        ), patch.object(
            bridge, "_config_path", return_value=str(self.config)
        ), patch.object(
            bridge,
            "_pi_runtime_settings",
            return_value=("main", "/home/ubuntu/src/pi-remote-runtime", str(session_root), str(job_root)),
        ), patch.object(
            bridge, "_pi_control_command", side_effect=fake_pi
        ), patch.object(bridge, "_audit"):
            first = bridge.start_pi_step(
                "Multi-step goal",
                "ferro",
                [{"role": "planner", "tool": "read", "params": {"path": "README.md"}}],
            )
            bridge.checkpoint_job(
                first["runtimeJobId"],
                "First step settled.",
                status="WAITING",
            )
            second = bridge.start_pi_step(
                "Multi-step goal",
                "ferro",
                [{"role": "planner", "tool": "read", "params": {"path": "pyproject.toml"}}],
                runtime_job_id=first["runtimeJobId"],
            )
            durable = bridge.get_job(first["runtimeJobId"])

        self.assertEqual(second["runtimeJobId"], first["runtimeJobId"])
        self.assertEqual(second["sessionId"], first["sessionId"])
        self.assertEqual(durable["backend"]["job_id"], second["jobId"])
        self.assertEqual(durable["backend"]["session_file"], str(session_file))
        self.assertEqual([call[0] for call in calls], ["create", "job-start", "state", "job-start"])

    def test_start_pi_step_rejects_secret_fields_before_launch(self) -> None:
        with patch.object(bridge, "_pi_control_command") as control:
            with self.assertRaises(PermissionError):
                bridge.start_pi_step(
                    "Unsafe",
                    "ferro",
                    [{"role": "coder", "tool": "write", "params": {"api_key": "nope"}}],
                )
        control.assert_not_called()

    def test_credential_broker_exposes_only_handles_and_leases(self) -> None:
        root = Path(self.tmp.name) / "credentials"
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_CREDENTIALS": str(root)},
        ), patch.object(bridge, "_config_path", return_value=str(self.config)):
            credentials.set_local_secret(
                "github.production",
                "never-return-this-value",
                provider="github",
                capabilities=["git_push"],
                projects=["ferro"],
                devices=["main"],
            )
            listed = bridge.list_credentials()
            lease = bridge.lease_credential(
                "github.production",
                "git_push",
                project="ferro",
                ttl_seconds=60,
            )
            leases = bridge.list_credential_leases()
            revoked = bridge.revoke_credential_lease(lease["lease_id"])

        public_blob = json.dumps(
            {"listed": listed, "lease": lease, "leases": leases, "revoked": revoked}
        )
        self.assertNotIn("never-return-this-value", public_blob)
        self.assertEqual(lease["device"], "main")
        self.assertEqual(lease["project"], "ferro")
        self.assertFalse(revoked["active"])

    def test_github_identity_consumes_scoped_lease_without_returning_secret(self) -> None:
        root = Path(self.tmp.name) / "credentials"

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size):
                return json.dumps({
                    "login": "octocat",
                    "id": 1,
                    "name": "The Octocat",
                    "type": "User",
                }).encode("utf-8")

        class FakeOpener:
            def open(self, request, timeout=10):
                self.authorization = request.headers.get("Authorization")
                return FakeResponse()

        fake = FakeOpener()
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_CREDENTIALS": str(root)},
        ), patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(
            bridge.urllib.request, "build_opener", return_value=fake
        ):
            credentials.set_local_secret(
                "github.production",
                "never-return-this-value",
                provider="github",
                capabilities=["github.identity"],
                projects=["ferro"],
                devices=["main"],
            )
            lease = bridge.lease_credential(
                "github.production",
                "github.identity",
                project="ferro",
                ttl_seconds=60,
            )
            result = bridge.github_identity(
                lease["lease_id"],
                project="ferro",
            )

        self.assertEqual(result["login"], "octocat")
        self.assertTrue(result["authenticated"])
        self.assertIn("never-return-this-value", fake.authorization)
        self.assertNotIn("never-return-this-value", json.dumps(result))

    def test_remote_overview_is_secret_free_and_summarizes_control_plane(self) -> None:
        jobs_root = Path(self.tmp.name) / "jobs"
        credentials_root = Path(self.tmp.name) / "credentials"
        with patch.dict(
            os.environ,
            {
                "LIVINGRUNTIME_REMOTE_JOBS": str(jobs_root),
                "LIVINGRUNTIME_REMOTE_CREDENTIALS": str(credentials_root),
                "LIVINGRUNTIME_REMOTE_EXECUTION_STATE": str(Path(self.tmp.name) / "execution-state.json"),
            },
        ), patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(
            bridge, "_ssh", self._ssh
        ), patch.object(
            bridge, "exec_permission_snapshot",
            return_value={"pending": [], "grants": [], "revoked": []},
        ), patch.object(
            bridge, "_recent_audit_events",
            return_value=[{"ts": 1.0, "tool": "connection_status", "ok": True}],
        ):
            credentials.set_local_secret(
                "github.production",
                "never-return-this-value",
                provider="github",
                capabilities=["github.identity"],
            )
            bridge.create_job("Long task", project="ferro")
            result = bridge.remote_overview()

        self.assertEqual(result["version"], PLUGIN_VERSION)
        self.assertEqual(len(result["devices"]["devices"]), 1)
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(len(result["credentials"]), 1)
        self.assertEqual(result["permissions"]["pending"], [])
        self.assertEqual(result["execution"]["source_of_truth"], "server_receipt")
        self.assertEqual(result["execution"]["state"], "WAITING")
        self.assertEqual(result["execution"]["active_job_count"], 1)
        self.assertEqual(result["execution"]["worker_alive_count"], 0)
        self.assertTrue(result["execution"]["running_requires_live_process"])
        self.assertNotIn("never-return-this-value", json.dumps(result))

    def test_openai_continuation_binding_and_terminal_resume(self) -> None:
        with patch.object(bridge.Path, "home", return_value=Path(self.tmp.name)):
            bound = bridge.bind_openai_pi_continuation(
                "session-a", "job-a", "/home/ubuntu/src/pi-remote", None
            )
            self.assertEqual(
                bound["hookSpecificOutput"]["hookEventName"], "PostToolUse"
            )
            runtime_job_id = bound["runtimeJobId"]
            self.assertEqual(bridge.get_job(runtime_job_id)["status"], "PENDING")
            with patch.object(
                bridge,
                "_pi_job_command",
                return_value={"status": "SUCCEEDED"},
            ):
                decision = bridge.continue_openai_pi_job("session-a", timeout_seconds=5)
            self.assertEqual(decision["decision"], "block")
            self.assertIn("job-a", decision["reason"])
            self.assertIn("SUCCEEDED", decision["reason"])
            self.assertEqual(bridge.get_job(runtime_job_id)["status"], "SUCCEEDED")
            self.assertEqual(
                bridge.continue_openai_pi_job("session-a", timeout_seconds=1),
                {"continue": True},
            )

    def test_openai_interrupt_cancels_pi_and_terminalizes_goal_by_user(self) -> None:
        with patch.object(bridge.Path, "home", return_value=Path(self.tmp.name)):
            bound = bridge.bind_openai_pi_continuation(
                "session-stop", "job-stop", "/home/ubuntu/src/pi-remote", None
            )
            runtime_job_id = bound["runtimeJobId"]
            calls = []
            with patch.object(
                bridge,
                "_pi_job_command",
                side_effect=lambda command, *args, **kwargs: (
                    calls.append(command)
                    or {"status": "CANCEL_REQUESTED", "cancelSignalSent": True}
                ),
            ):
                decision = bridge.continue_openai_pi_job(
                    "session-stop",
                    timeout_seconds=3,
                    interrupted=True,
                )
            self.assertEqual(calls, ["job-cancel"])
            self.assertFalse(decision["continue"])
            self.assertTrue(decision["cancelledByUser"])
            self.assertEqual(
                bridge.get_job(runtime_job_id)["status"],
                "CANCELLED_BY_USER",
            )
            self.assertEqual(
                bridge.continue_openai_pi_job("session-stop", timeout_seconds=1),
                {"continue": True},
            )

    def test_recursive_stop_hook_never_reblocks(self) -> None:
        with patch.object(bridge.Path, "home", return_value=Path(self.tmp.name)):
            bridge.bind_openai_pi_continuation(
                "session-recursive", "job-recursive", "/home/ubuntu/src/pi-remote", None
            )
            result = bridge.continue_openai_pi_job(
                "session-recursive",
                timeout_seconds=1,
                stop_hook_active=True,
            )
            self.assertFalse(result["continue"])
            self.assertIn("exhausted", result["stopReason"])


    def test_exec_refuses_long_synchronous_wait_and_points_to_supervisor(self) -> None:
        with patch.object(bridge, "_audit") as audit, patch.object(
            bridge, "_remote"
        ) as remote:
            result = bridge.exec(
                ["python3", "-m", "pytest", "-q"],
                timeout_seconds=120,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["long_job_required"])
        self.assertEqual(result["suggested_tool"], "start_long_job")
        self.assertEqual(result["requested_timeout_seconds"], 120)
        self.assertIn("heartbeat", result["reason"])
        remote.assert_not_called()
        audit.assert_called_once()


if __name__ == "__main__":
    unittest.main()

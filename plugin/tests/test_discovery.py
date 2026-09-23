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
            "list_projects": (True, False, False),
            "read_file": (True, False, False),
            "apply_patch": (False, True, False),
            "git": (False, True, True),
            "logs": (True, False, False),
            "list_credentials": (True, False, False),
            "lease_credential": (False, False, False),
            "list_credential_leases": (True, False, False),
            "revoke_credential_lease": (False, True, False),
            "create_job": (False, False, False),
            "get_job": (True, False, False),
            "list_jobs": (True, False, False),
            "checkpoint_job": (False, False, False),
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
                status="RUNNING",
            )
            recovered = bridge.get_job(created["job_id"])
            listed = bridge.list_jobs(status="RUNNING")

        self.assertEqual(checkpointed["status"], "RUNNING")
        self.assertEqual(recovered["next_action"], "apply patch")
        self.assertEqual(listed["jobs"][0]["job_id"], created["job_id"])

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

    def test_openai_continuation_binding_and_terminal_resume(self) -> None:
        with patch.object(bridge.Path, "home", return_value=Path(self.tmp.name)):
            bound = bridge.bind_openai_pi_continuation(
                "session-a", "job-a", "/home/ubuntu/src/pi-remote", None
            )
            self.assertEqual(
                bound["hookSpecificOutput"]["hookEventName"], "PostToolUse"
            )
            runtime_job_id = bound["runtimeJobId"]
            self.assertEqual(bridge.get_job(runtime_job_id)["status"], "RUNNING")
            with patch.object(
                bridge,
                "_pi_job_command",
                return_value={
                    "terminal": True,
                    "timedOut": False,
                    "state": {"status": "SUCCEEDED"},
                },
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


if __name__ == "__main__":
    unittest.main()

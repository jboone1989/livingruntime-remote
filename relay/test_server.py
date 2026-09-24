from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("LIVINGRUNTIME_RELAY_ISSUER", "https://auth.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_AUDIENCE", "https://remote.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_JWKS_URL", "https://auth.example.test/jwks.json")
os.environ.setdefault("LIVINGRUNTIME_RELAY_RESOURCE_URL", "https://remote.example.test/mcp")
os.environ.setdefault("LIVINGRUNTIME_RELAY_DB", str(Path(tempfile.gettempdir()) / "lr-relay-test.sqlite3"))
os.environ.setdefault("OPENAI_APPS_CHALLENGE", "challenge-token")

SCRIPTS = Path(__file__).resolve().parents[1] / "plugin" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from starlette.testclient import TestClient

from contract import REMOTE_TOOLS
import server
from store import RelayStore


class RelayServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.demo_path = Path(self.tmp.name) / "demo.mp4"
        self.demo_path.write_bytes(b"fake-mp4")
        os.environ["LIVINGRUNTIME_DEMO_RECORDING_PATH"] = str(self.demo_path)
        self.store = RelayStore(str(Path(self.tmp.name) / "relay.sqlite3"))
        self.app = server.create_app(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_challenge_and_oauth_resource_metadata(self):
        with TestClient(self.app) as client:
            health = client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["version"], "0.4.24")
            challenge = client.get("/.well-known/openai-apps-challenge")
            self.assertEqual(challenge.text, "challenge-token")
            meta = client.get("/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(meta.status_code, 200)
            data = meta.json()
            self.assertEqual(data["resource"], "https://remote.example.test/mcp")
            self.assertEqual(data["authorization_servers"], ["https://auth.example.test"])

    def test_install_page_and_bootstrap_redirects(self):
        with TestClient(self.app, follow_redirects=False) as client:
            page = client.get("/install")
            self.assertEqual(page.status_code, 200)
            self.assertIn("LivingRuntime Remote Connector", page.text)
            self.assertIn("/install.ps1", page.text)
            self.assertIn("/install.sh", page.text)

            ps1 = client.get("/install.ps1")
            self.assertEqual(ps1.status_code, 302)
            self.assertIn("install-connector.ps1", ps1.headers["location"])

            sh = client.get("/install.sh")
            self.assertEqual(sh.status_code, 302)
            self.assertIn("install-connector.sh", sh.headers["location"])

            binary = client.get("/download/windows-amd64")
            self.assertEqual(binary.status_code, 302)
            self.assertTrue(binary.headers["location"].endswith(
                "livingruntime-remote-connector-windows-amd64.exe"
            ))
            missing = client.get("/download/plan9-amd64")
            self.assertEqual(missing.status_code, 404)

    def test_public_home_legal_and_support_pages(self):
        with TestClient(self.app) as client:
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn("LivingRuntime Remote", home.text)
            self.assertIn("/privacy", home.text)
            self.assertIn("/terms", home.text)
            self.assertIn("/support", home.text)

            privacy = client.get("/privacy")
            self.assertEqual(privacy.status_code, 200)
            self.assertIn("Privacy Policy", privacy.text)
            self.assertIn("does not store SSH passwords", privacy.text)
            self.assertIn("OAuth passwords are stored only as salted scrypt hashes", privacy.text)
            self.assertIn("stale task records are eligible for cleanup after 24 hours", privacy.text)

            terms = client.get("/terms")
            self.assertEqual(terms.status_code, 200)
            self.assertIn("Terms of Service", terms.text)
            self.assertIn("authorized to access", terms.text)

            support = client.get("/support")
            self.assertEqual(support.status_code, 200)
            self.assertIn("connection_status", support.text)
            self.assertIn("capabilities", support.text)

            demo = client.get("/demo")
            self.assertEqual(demo.status_code, 200)
            self.assertIn("/demo.mp4", demo.text)
            recording = client.get("/demo.mp4")
            self.assertEqual(recording.status_code, 200)
            self.assertEqual(recording.headers["content-type"], "video/mp4")

    def test_unauthenticated_mcp_returns_oauth_resource_challenge(self):
        with TestClient(self.app) as client:
            response = client.post("/mcp", json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            })
            self.assertEqual(response.status_code, 401)
            challenge = response.headers.get("www-authenticate", "")
            self.assertIn("Bearer", challenge)
            self.assertIn("resource_metadata=", challenge)
            self.assertIn("/.well-known/oauth-protected-resource/mcp", challenge)

    def test_device_http_flow_is_bound_to_device_token(self):
        code = self.store.create_pairing_code("user-a")["code"]
        with TestClient(self.app) as client:
            paired = client.post("/device/pair", json={"code": code, "name": "test"}).json()
            headers = {"authorization": "Bearer " + paired["device_token"]}
            task_id = self.store.enqueue("user-a", paired["device_id"], "connection_status", {})
            polled = client.post("/device/poll?wait=0.01", json={}, headers=headers)
            self.assertEqual(polled.status_code, 200)
            self.assertEqual(polled.json()["task"]["task_id"], task_id)
            done = client.post("/device/result", json={
                "task_id": task_id, "result": {"ok": True, "result": {"ok": True}}
            }, headers=headers)
            self.assertEqual(done.status_code, 200)
            self.assertTrue(self.store.result("user-a", task_id)["ok"])
            denied = client.post("/device/poll?wait=0", json={},
                                 headers={"authorization": "Bearer wrong"})
            self.assertEqual(denied.status_code, 401)

    def test_idle_long_poll_does_not_spin_on_sqlite(self):
        class CountingStore(RelayStore):
            def __init__(self, path: str) -> None:
                super().__init__(path)
                self.claim_calls = 0

            def claim(self, device_id: str):
                self.claim_calls += 1
                return super().claim(device_id)

        store = CountingStore(str(Path(self.tmp.name) / "counting.sqlite3"))
        app = server.create_app(store)
        code = store.create_pairing_code("user-a")["code"]
        with TestClient(app) as client:
            paired = client.post("/device/pair", json={"code": code, "name": "test"}).json()
            headers = {"authorization": "Bearer " + paired["device_token"]}
            started = time.monotonic()
            response = client.post("/device/poll?wait=0.3", json={}, headers=headers)
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["task"])
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertEqual(store.claim_calls, 1)

    def test_task_notification_wakes_waiter_without_polling(self):
        relay = server.Relay(self.store)

        async def exercise() -> float:
            relay.arm_task_wait("device-a")
            started = time.monotonic()
            waiter = asyncio.create_task(relay.wait_for_task("device-a", 1.0))
            await asyncio.sleep(0.05)
            relay.notify_task("device-a")
            self.assertTrue(await waiter)
            return time.monotonic() - started

        elapsed = asyncio.run(exercise())
        self.assertLess(elapsed, 0.5)

    def test_result_wait_does_not_poll_sqlite(self):
        class CountingStore(RelayStore):
            def __init__(self, path: str) -> None:
                super().__init__(path)
                self.result_calls = 0

            def result(self, user_sub: str, task_id: str, consume: bool = False):
                self.result_calls += 1
                return super().result(user_sub, task_id, consume=consume)

        store = CountingStore(str(Path(self.tmp.name) / "result-counting.sqlite3"))
        paired = store.pair_device(
            store.create_pairing_code("user-a")["code"], "test"
        )
        relay = server.Relay(
            store, timeout=1.0, reconnect_grace=0, device_stale_after=30
        )

        async def exercise():
            call = asyncio.create_task(
                relay.call("user-a", "connection_status", {})
            )
            await asyncio.sleep(0.05)
            task = store.claim(paired["device_id"])
            self.assertIsNotNone(task)
            await asyncio.sleep(0.35)
            self.assertEqual(store.result_calls, 1)
            store.complete(
                paired["device_id"],
                task["task_id"],
                {"ok": True, "result": {"ok": True}},
            )
            relay.notify_result(task["task_id"])
            return await call

        result = asyncio.run(exercise())
        self.assertTrue(result["ok"])
        self.assertEqual(store.result_calls, 2)

    def test_pair_endpoint_rate_limits_repeated_failures(self):
        with TestClient(self.app) as client:
            headers = {"x-forwarded-for": "203.0.113.10"}
            for _ in range(10):
                response = client.post(
                    "/device/pair",
                    json={"code": "WRONG-CODE", "name": "test"},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 401)
            limited = client.post(
                "/device/pair",
                json={"code": "WRONG-CODE", "name": "test"},
                headers=headers,
            )
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers.get("retry-after"), "60")

    def test_relay_reports_stale_device_and_does_not_enqueue_work(self):
        paired = self.store.pair_device(
            self.store.create_pairing_code("user-a")["code"], "test"
        )
        with self.store.db() as db:
            db.execute(
                "UPDATE devices SET last_seen=? WHERE device_id=?",
                (time.time() - 120, paired["device_id"]),
            )
        relay = server.Relay(
            self.store, timeout=0.01, reconnect_grace=0, device_stale_after=30
        )
        status = relay.device_status("user-a")
        self.assertFalse(status["online"])
        with self.assertRaisesRegex(RuntimeError, "offline"):
            asyncio.run(relay.call("user-a", "connection_status", {}))
        with self.store.db() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_public_tools_do_not_forward_function_locals(self):
        source = inspect.getsource(server.create_mcp)
        self.assertNotIn("locals()", source)

    def test_public_mcp_declares_expected_tools_and_security(self):
        mcp = server.create_mcp(server.Relay(self.store))
        tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
        expected = {
            "create_pairing_code",
            "device_status",
            "disconnect_device",
            "watch_pi_job",
            "wait_pi_job_completion",
            "bind_openai_pi_continuation",
            "continue_openai_pi_job",
        } | set(REMOTE_TOOLS)
        self.assertEqual(set(tools), expected)
        for tool in tools.values():
            self.assertTrue(tool.title, tool.name)
            self.assertTrue(tool.description, tool.name)
            self.assertIsNotNone(tool.output_schema, tool.name)
        self.assertTrue(tools["read_file"].annotations.read_only_hint)
        self.assertFalse(tools["write_file"].annotations.read_only_hint)
        self.assertTrue(tools["git"].annotations.open_world_hint)
        self.assertTrue(tools["systemd"].annotations.destructive_hint)
        self.assertIn("list_devices", tools)
        for name in {
            "connection_status", "read_file", "write_file", "list_dir", "git",
            "exec", "process", "systemd", "logs", "apply_patch",
        }:
            schema = tools[name].parameters or {}
            self.assertIn("device", schema.get("properties", {}), name)
        self.assertEqual(
            tools["read_file"].meta["securitySchemes"][0]["scopes"],
            ["remote:read", "openid", "email"],
        )
        self.assertEqual(
            tools["git"].meta["securitySchemes"][0]["scopes"],
            ["remote:read", "remote:write", "openid", "email"],
        )
        self.assertEqual(
            tools["start_long_job"].meta["ui"]["resourceUri"],
            server.LONG_JOB_WIDGET_URI,
        )
        self.assertEqual(
            tools["start_long_job"].meta["ui"]["visibility"],
            ["model", "app"],
        )
        self.assertEqual(
            tools["watch_long_job"].meta["ui"]["resourceUri"],
            server.LONG_JOB_WIDGET_URI,
        )
        self.assertEqual(
            tools["watch_long_job"].meta["ui"]["visibility"],
            ["model", "app"],
        )
        self.assertEqual(
            tools["wait_long_job"].meta["ui"]["visibility"],
            ["app"],
        )
        self.assertNotIn("resourceUri", tools["wait_long_job"].meta["ui"])
        self.assertEqual(
            tools["watch_pi_job"].meta["ui"]["resourceUri"],
            server.PI_JOB_WIDGET_URI,
        )
        self.assertEqual(
            tools["watch_pi_job"].meta["ui"]["visibility"],
            ["model", "app"],
        )
        self.assertEqual(
            tools["start_pi_step"].meta["ui"]["resourceUri"],
            server.PI_JOB_WIDGET_URI,
        )
        self.assertEqual(
            tools["start_pi_step"].meta["ui"]["visibility"],
            ["model", "app"],
        )
        self.assertTrue(tools["start_pi_step"].annotations.destructive_hint)
        self.assertFalse(tools["start_pi_step"].annotations.open_world_hint)
        self.assertEqual(
            tools["wait_pi_job_completion"].meta["ui"]["visibility"],
            ["app"],
        )
        self.assertNotIn("resourceUri", tools["wait_pi_job_completion"].meta["ui"])
        self.assertEqual(
            tools["remote_overview"].meta["ui"]["resourceUri"],
            server.CONTROL_PLANE_WIDGET_URI,
        )
        self.assertEqual(
            tools["remote_overview"].meta["ui"]["visibility"],
            ["model", "app"],
        )
        self.assertIn(
            "include_resources",
            (tools["list_devices"].parameters or {}).get("properties", {}),
        )
        self.assertTrue(tools["remote_overview"].annotations.read_only_hint)
        self.assertFalse(tools["github_identity"].annotations.read_only_hint)
        self.assertTrue(tools["github_identity"].annotations.open_world_hint)
        self.assertFalse(
            tools["bind_openai_pi_continuation"].annotations.destructive_hint
        )
        self.assertFalse(
            tools["continue_openai_pi_job"].annotations.read_only_hint
        )
        resources = asyncio.run(mcp.list_resources())
        widget = next(
            resource
            for resource in resources
            if str(resource.uri) == server.PI_JOB_WIDGET_URI
        )
        widget_meta = widget.model_dump(by_alias=True)["_meta"]["ui"]
        self.assertEqual(widget_meta["domain"], server.PI_JOB_WIDGET_DOMAIN)
        self.assertEqual(
            widget_meta["csp"],
            {"connectDomains": [], "resourceDomains": []},
        )
        long_widget = next(
            resource
            for resource in resources
            if str(resource.uri) == server.LONG_JOB_WIDGET_URI
        )
        long_meta = long_widget.model_dump(by_alias=True)["_meta"]["ui"]
        self.assertEqual(long_meta["domain"], server.PI_JOB_WIDGET_DOMAIN)
        self.assertEqual(
            long_meta["csp"],
            {"connectDomains": [], "resourceDomains": []},
        )
        control_widget = next(
            resource
            for resource in resources
            if str(resource.uri) == server.CONTROL_PLANE_WIDGET_URI
        )
        control_meta = control_widget.model_dump(by_alias=True)["_meta"]["ui"]
        self.assertEqual(control_meta["domain"], server.PI_JOB_WIDGET_DOMAIN)
        self.assertEqual(
            control_meta["csp"],
            {"connectDomains": [], "resourceDomains": []},
        )

    def test_pi_job_widget_uses_event_wait_and_same_conversation_followup(self):
        html = server.PI_JOB_WIDGET_HTML
        self.assertIn('"tools/call"', html)
        self.assertIn('"wait_pi_job_completion"', html)
        self.assertIn("timeout_seconds: 30", html)
        self.assertIn('"ui/message"', html)
        self.assertIn('"ui/update-model-context"', html)
        self.assertIn('"ui/initialize"', html)
        self.assertIn("runtime_job_id=", html)
        self.assertIn("Durable LivingRuntime goal", html)
        self.assertIn("Remote connection lost", html)
        self.assertNotIn("setInterval(", html)
        self.assertNotIn("job-status", html)

    def test_long_job_widget_uses_bounded_wait_and_reports_stall(self):
        html = server.LONG_JOB_WIDGET_HTML
        self.assertIn('"tools/call"', html)
        self.assertIn('"wait_long_job"', html)
        self.assertIn("timeout_seconds:30", html)
        self.assertIn("STALLED", html)
        self.assertIn("longJobProgress", html)
        self.assertIn("heartbeatAgeSeconds", html)
        self.assertIn("progressAgeSeconds", html)
        self.assertIn('"ui/message"', html)
        self.assertIn('"ui/update-model-context"', html)
        self.assertNotIn("setInterval(", html)

    def test_control_plane_widget_is_read_only_snapshot_ui(self):
        html = server.CONTROL_PLANE_WIDGET_HTML
        self.assertIn("LivingRuntime Remote Control Plane", html)
        self.assertIn('"remote_overview"', html)
        self.assertIn("Durable jobs", html)
        self.assertIn("Permissions & credentials", html)
        self.assertNotIn("approve_exec_permission", html)
        self.assertNotIn("lease_credential", html)

    def test_public_openai_continuation_does_not_duplicate_widget_wait(self):
        source = inspect.getsource(server.create_mcp)
        self.assertIn("bind_openai_pi_continuation", source)
        self.assertIn("continue_openai_pi_job", source)
        self.assertIn('"job-status"', source)
        self.assertIn('"watch_mode": "apps_sdk_widget"', source)
        self.assertIn('"continue": True', source)
        self.assertIn('"decision": "block"', source)
        self.assertIn("clear_continuation", source)


if __name__ == "__main__":
    unittest.main()

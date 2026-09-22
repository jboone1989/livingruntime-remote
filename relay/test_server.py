from __future__ import annotations

import asyncio
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
        self.store = RelayStore(str(Path(self.tmp.name) / "relay.sqlite3"))
        self.app = server.create_app(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_challenge_and_oauth_resource_metadata(self):
        with TestClient(self.app) as client:
            health = client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["version"], "0.4.6")
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

            terms = client.get("/terms")
            self.assertEqual(terms.status_code, 200)
            self.assertIn("Terms of Service", terms.text)
            self.assertIn("authorized to access", terms.text)

            support = client.get("/support")
            self.assertEqual(support.status_code, 200)
            self.assertIn("connection_status", support.text)
            self.assertIn("capabilities", support.text)

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

    def test_public_mcp_declares_expected_tools_and_security(self):
        mcp = server.create_mcp(server.Relay(self.store))
        tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
        expected = {"create_pairing_code", "device_status", "disconnect_device"} | set(REMOTE_TOOLS)
        self.assertEqual(set(tools), expected)
        self.assertTrue(tools["read_file"].annotations.read_only_hint)
        self.assertFalse(tools["write_file"].annotations.read_only_hint)
        self.assertTrue(tools["git"].annotations.open_world_hint)
        self.assertTrue(tools["systemd"].annotations.destructive_hint)
        self.assertEqual(
            tools["read_file"].meta["securitySchemes"][0]["scopes"],
            ["remote:read", "openid", "email"],
        )
        self.assertEqual(
            tools["git"].meta["securitySchemes"][0]["scopes"],
            ["remote:read", "remote:write", "openid", "email"],
        )


if __name__ == "__main__":
    unittest.main()

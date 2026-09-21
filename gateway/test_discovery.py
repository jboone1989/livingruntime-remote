from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

CORE_TOOLS = ("connection_status", "read_file", "git", "logs")
GATEWAY = Path(__file__).resolve().parent


class GatewayDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "allowed"
        root.mkdir()
        os.environ["MCP_GATEWAY_TOKEN"] = "test-token-not-a-secret-for-prod"
        os.environ["MCP_GATEWAY_ALLOWED_ROOTS"] = str(root)
        os.environ["MCP_GATEWAY_SYSTEMD_UNITS"] = "content-agent.service"
        os.environ["MCP_GATEWAY_AUDIT_LOG"] = str(Path(self.tmp.name) / "audit.jsonl")
        os.environ["MCP_GATEWAY_BIND_HOST"] = "127.0.0.1"
        sys.path.insert(0, str(GATEWAY))
        for name in list(sys.modules):
            if name in {"server", "policy"} or name.startswith("server."):
                sys.modules.pop(name)
        self.server = importlib.import_module("server")
        self.root = root

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tools_list_includes_acceptance_names(self) -> None:
        names = self._tool_names()
        missing = [name for name in CORE_TOOLS if name not in names]
        self.assertEqual(missing, [], msg=f"gateway tools/list missing {missing}; got {names}")
        self.assertIn("gateway_status", names)

    def test_connection_status_does_not_return_token(self) -> None:
        (self.root / "keep.txt").write_text("ok", encoding="utf-8")
        status = self.server.connection_status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["endpoint"]["reachable"])
        self.assertEqual(status["endpoint"]["bind"], "127.0.0.1:8765")
        self.assertFalse(status["endpoint"]["public_ingress"])
        self.assertTrue(status["connectivity"]["authenticated"])
        self.assertTrue(status["connectivity"]["authorized"])
        self.assertIsNotNone(status["connectivity"]["latency_ms"])
        blob = json.dumps(status)
        self.assertNotIn("test-token-not-a-secret-for-prod", blob)
        self.assertNotIn("MCP_GATEWAY_TOKEN", blob)

    def test_core_tool_annotations(self) -> None:
        by_name = {tool.name: tool for tool in self._tools()}
        self.assertTrue(by_name["connection_status"].annotations.read_only_hint)
        self.assertTrue(by_name["read_file"].annotations.read_only_hint)
        self.assertTrue(by_name["logs"].annotations.read_only_hint)
        self.assertFalse(by_name["git"].annotations.read_only_hint)
        self.assertTrue(by_name["git"].annotations.destructive_hint)

    def test_healthz_stays_on_loopback_app(self) -> None:
        from starlette.testclient import TestClient

        client = TestClient(self.server.app)
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def _tools(self):
        mcp = self.server.mcp
        for attr in ("_tool_manager", "tool_manager"):
            manager = getattr(mcp, attr, None)
            if manager is not None and hasattr(manager, "list_tools"):
                return manager.list_tools()
        tools = getattr(mcp, "tools", None)
        if tools is not None:
            return list(tools.values()) if isinstance(tools, dict) else list(tools)
        raise RuntimeError("cannot inspect MCPServer tools")

    def _tool_names(self) -> list[str]:
        names = []
        for tool in self._tools():
            names.append(getattr(tool, "name", tool))
        return sorted(str(name) for name in names)


if __name__ == "__main__":
    unittest.main()

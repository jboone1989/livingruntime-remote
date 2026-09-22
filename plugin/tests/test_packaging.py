from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from contract import PLUGIN_VERSION, REMOTE_IDENTITY, REMOTE_TOOLS  # noqa: E402


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_portable_manifest_points_at_openai_apps_mapping(self) -> None:
        plugin = json.loads((self.root / "plugin.json").read_text(encoding="utf-8"))
        openai = plugin["extensions"]["com.openai"]
        self.assertEqual(plugin["name"], "livingruntime-remote")
        self.assertEqual(plugin["version"], PLUGIN_VERSION)
        self.assertEqual(PLUGIN_VERSION, "0.4.7")
        self.assertEqual(REMOTE_IDENTITY, "livingruntime.remote")
        self.assertEqual(openai["apps"], "./.app.json")
        self.assertEqual(openai["interface"]["displayName"], "LivingRuntime Remote")
        self.assertTrue((self.root / "assets" / "logo.png").exists())

    def test_app_mapping_does_not_invent_a_chatgpt_id(self) -> None:
        apps = json.loads((self.root / ".app.json").read_text(encoding="utf-8"))
        self.assertEqual(apps, {"apps": {}})
        example = json.loads((self.root / "app.example.json").read_text(encoding="utf-8"))
        app_id = example["apps"]["livingruntime_remote"]["id"]
        self.assertTrue(app_id.startswith("plugin_asdk_app_"))
        self.assertIn("REPLACE", app_id)

    def test_mcp_json_keeps_stdio_and_documents_http(self) -> None:
        mcp = json.loads((self.root / "mcp.json").read_text(encoding="utf-8"))
        stdio = mcp["mcpServers"]["livingruntime_remote"]
        self.assertEqual(stdio["type"], "stdio")
        self.assertEqual(stdio["command"], "python")
        self.assertEqual(stdio["args"], ["./scripts/launcher.py"])
        self.assertNotIn("livingruntime_remote_http", mcp["mcpServers"])
        http = json.loads((self.root / "mcp.http.json").read_text(encoding="utf-8"))
        self.assertEqual(http["mcpServers"]["livingruntime_remote_http"]["type"], "streamable-http")
        self.assertEqual(http["mcpServers"]["livingruntime_remote_http"]["url"], "http://127.0.0.1:8766/mcp")

    def test_codex_overlay_references_apps_and_mcp(self) -> None:
        overlay = json.loads((self.root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(overlay["apps"], "./.app.json")
        self.assertEqual(overlay["mcpServers"], "./.mcp.json")
        self.assertEqual(overlay["version"], PLUGIN_VERSION)

    def test_submission_covers_remote_tools(self) -> None:
        submission = json.loads((self.root / "chatgpt-app-submission.json").read_text(encoding="utf-8"))
        for name in REMOTE_TOOLS:
            self.assertIn(name, submission["tools"])
            hints = submission["tools"][name]["annotations"]
            self.assertIn("readOnlyHint", hints)
            self.assertIn("openWorldHint", hints)
            self.assertIn("destructiveHint", hints)
        positives = [case["tools_triggered"] for case in submission["test_cases"]]
        for name in ("connection_status", "capabilities", "diagnostics", "apply_patch", "read_file", "git", "logs"):
            self.assertIn(name, positives)
        self.assertGreaterEqual(len(submission["test_cases"]), 6)
        self.assertEqual(len(submission["negative_test_cases"]), 3)

    def test_windows_tunnel_launchers_exist(self) -> None:
        self.assertTrue((self.root / "scripts" / "start-tunnel.cmd").exists())
        self.assertTrue((self.root / "scripts" / "start-tunnel.ps1").exists())
        self.assertTrue((self.root / "scripts" / "companion.py").exists())
        self.assertTrue((self.root / "scripts" / "install-host.py").exists())
        self.assertTrue((self.root / "scripts" / "install-personal.py").exists())
        self.assertTrue((self.root / "scripts" / "install-personal.ps1").exists())
        self.assertTrue((self.root / "scripts" / "connector.py").exists())
        self.assertTrue((self.root / "scripts" / "install-connector.ps1").exists())
        self.assertTrue((self.root / "scripts" / "install-connector.sh").exists())
        self.assertTrue((self.root / "docs" / "livingruntime-remote-release-checklist.md").exists())

    def test_plugin_tree_has_no_embedded_secrets(self) -> None:
        forbidden = ["BEGIN PRIVATE " + "KEY", "BEGIN OPENSSH PRIVATE " + "KEY", "sk-proj" + "-", "password" + "="]
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix in {".png", ".ico", ".pyc"}:
                continue
            if "__pycache__" in path.parts or path.name in {"test_packaging.py", "test_companion.py"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in forbidden:
                self.assertNotIn(marker, text, msg=path)


if __name__ == "__main__":
    unittest.main()

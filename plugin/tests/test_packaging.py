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
        self.assertEqual(PLUGIN_VERSION, "0.4.20")
        self.assertEqual(REMOTE_IDENTITY, "livingruntime.remote")
        self.assertEqual(openai["apps"], "./.app.json")
        self.assertEqual(openai["hooks"], "./hooks/hooks.json")
        self.assertEqual(openai["interface"]["displayName"], "LivingRuntime Remote")
        self.assertEqual(openai["interface"]["supportURL"], "https://remote.livingruntime.com/support")
        self.assertTrue((self.root / "assets" / "logo.png").exists())

    def test_openai_listing_fields_meet_directory_limits(self) -> None:
        plugin = json.loads((self.root / "plugin.json").read_text(encoding="utf-8"))
        interface = plugin["extensions"]["com.openai"]["interface"]
        self.assertLessEqual(len(interface["displayName"]), 30)
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        prompts = interface["defaultPrompt"]
        self.assertLessEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertLessEqual(len(prompt), 128)
            self.assertNotIn("@", prompt)
        color = interface["brandColor"]
        self.assertRegex(color, r"^#[0-9A-Fa-f]{6}$")

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
        self.assertEqual(overlay["hooks"], "./hooks/hooks.json")
        self.assertEqual(overlay["version"], PLUGIN_VERSION)

    def test_openai_hooks_bind_watch_and_continue_on_stop(self) -> None:
        hooks = json.loads((self.root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        post = hooks["hooks"]["PostToolUse"][0]
        self.assertEqual(post["matcher"], "watch_pi_job$")
        binder = post["hooks"][0]
        self.assertEqual(binder["type"], "mcp_tool")
        self.assertEqual(binder["server"], "livingruntime_remote")
        self.assertEqual(binder["tool"], "bind_openai_pi_continuation")
        self.assertEqual(binder["input"]["session_id"], "${session_id}")
        self.assertEqual(binder["input"]["job_id"], "${tool_input.job_id}")
        self.assertNotIn("pi_remote_dir", binder["input"])
        self.assertNotIn("job_root", binder["input"])

        stop = hooks["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(stop["type"], "mcp_tool")
        self.assertEqual(stop["server"], "livingruntime_remote")
        self.assertEqual(stop["tool"], "continue_openai_pi_job")
        self.assertEqual(stop["input"]["session_id"], "${session_id}")
        self.assertEqual(stop["input"]["timeout_seconds"], 540)
        self.assertEqual(stop["timeout"], 600)

    def test_submission_covers_public_tools_and_review_case_contract(self) -> None:
        submission = json.loads((self.root / "chatgpt-app-submission.json").read_text(encoding="utf-8"))
        expected_tools = set(REMOTE_TOOLS) | {
            "create_pairing_code",
            "device_status",
            "disconnect_device",
        }
        self.assertEqual(set(submission["tools"]), expected_tools)
        for name in expected_tools:
            hints = submission["tools"][name]["annotations"]
            self.assertIn("readOnlyHint", hints)
            self.assertIn("openWorldHint", hints)
            self.assertIn("destructiveHint", hints)

        positives = [case["tools_triggered"] for case in submission["test_cases"]]
        self.assertEqual(len(positives), 5)
        self.assertEqual(
            positives,
            ["connection_status", "list_projects", "read_file", "git", "write_file"],
        )
        self.assertEqual(len(submission["negative_test_cases"]), 3)
        self.assertTrue(all(case["tools_triggered"] is None for case in submission["negative_test_cases"]))

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

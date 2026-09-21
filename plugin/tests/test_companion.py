from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import companion  # noqa: E402
import tunnel  # noqa: E402


class CompanionTests(unittest.TestCase):
    def test_desktop_matcher_accepts_store_chatgpt(self) -> None:
        path = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.915.4065.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
        self.assertTrue(companion.is_chatgpt_desktop("ChatGPT.exe", path))
        self.assertTrue(companion.is_chatgpt_desktop("chatgpt.exe"))
        self.assertFalse(companion.is_chatgpt_desktop("codex.exe", path))
        self.assertFalse(companion.is_chatgpt_desktop("ChatGPT.exe", r"C:\Other\NotOpenAI\ChatGPT.exe"))

    def test_task_xml_starts_companion_watch_without_secrets(self) -> None:
        xml = companion.task_xml(
            Path(r"C:\Python311\pythonw.exe"),
            Path(r"C:\Users\me\.codex\plugins\livingruntime-remote\scripts\companion.py"),
            r"laptop\me",
        )
        self.assertIn("LivingRuntime Secure MCP Tunnel", xml)
        self.assertIn("--watch", xml)
        self.assertIn("pythonw.exe", xml)
        self.assertIn("companion.py", xml)
        self.assertIn("LogonTrigger", xml)
        self.assertIn("PT0S", xml)
        self.assertNotIn("sk-", xml)
        self.assertNotIn("CONTROL_PLANE_API_KEY", xml)

    def test_runtime_key_falls_back_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "control-plane.key"
            key_file.write_text("sk-test-runtime-key", encoding="utf-8")
            with patch.object(tunnel, "runtime_key_path", return_value=key_file):
                with patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": ""}, clear=False):
                    self.assertEqual(tunnel.load_runtime_key(), "sk-test-runtime-key")

    def test_ensure_tunnel_skips_start_when_ready(self) -> None:
        with patch.object(tunnel, "health_ready", return_value=True):
            with patch.object(tunnel, "start_tunnel_detached") as start:
                self.assertTrue(tunnel.ensure_tunnel_running())
                start.assert_not_called()

    def test_desktop_running_does_not_spawn_a_console(self) -> None:
        path = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.915.4065.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
        with patch.object(companion.os, "name", "nt"):
            with patch.object(tunnel, "list_windows_processes", return_value=[("ChatGPT.exe", 42)]):
                with patch.object(tunnel, "process_image_path", return_value=path):
                    with patch.object(companion.subprocess, "run") as run:
                        self.assertTrue(companion.desktop_running())
                        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

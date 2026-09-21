from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tunnel  # noqa: E402


class TunnelTests(unittest.TestCase):
    def test_quoted_mcp_command_uses_launcher_not_bash(self) -> None:
        command = tunnel.quoted_mcp_command(python=sys.executable)
        self.assertIn("launcher.py", command)
        self.assertNotIn("bash", command)
        self.assertTrue(command.startswith(sys.executable) or sys.executable in command or sys.executable.replace("\\", "/") in command)
        if os.name == "nt":
            self.assertNotIn("\\", command)

    def test_tunnel_id_rejects_api_keys(self) -> None:
        with self.assertRaises(SystemExit):
            tunnel.resolve_tunnel_id("sk-test-secret")
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "remote.json"
            config.write_text('{"tunnel_id":"tunnel_abc123def456"}', encoding="utf-8")
            with patch.object(tunnel, "config_path", return_value=config):
                with patch.dict(os.environ, {"CONTROL_PLANE_TUNNEL_ID": ""}, clear=False):
                    self.assertEqual(tunnel.resolve_tunnel_id(None), "tunnel_abc123def456")

    def test_http_bind_stays_on_loopback(self) -> None:
        self.assertEqual(tunnel.validate_http_bind("127.0.0.1"), "127.0.0.1")
        with self.assertRaises(ValueError):
            tunnel.validate_http_bind("0.0.0.0")
        with self.assertRaises(ValueError):
            tunnel.validate_http_bind("example.com")

    def test_init_argv_uses_stdio_by_default(self) -> None:
        argv = tunnel.build_init_argv(Path("tunnel-client"), "tunnel_abc", http=False, host="127.0.0.1", port=8766)
        self.assertIn("--mcp-command", argv)
        self.assertNotIn("--mcp-server-url", argv)
        self.assertIn("127.0.0.1:8080", argv)
        http_argv = tunnel.build_init_argv(Path("tunnel-client"), "tunnel_abc", http=True, host="127.0.0.1", port=8766)
        self.assertIn("http://127.0.0.1:8766/mcp", http_argv)
        with self.assertRaises(ValueError):
            tunnel.build_init_argv(
                Path("tunnel-client"),
                "tunnel_abc",
                http=False,
                host="127.0.0.1",
                port=8766,
                health_listen_addr="0.0.0.0:18080",
            )

    def test_choose_windows_exe_asset(self) -> None:
        assets = [
            {"name": "tunnel-client_linux_amd64", "browser_download_url": "https://example.com/linux"},
            {"name": "tunnel-client-v0.0.14-windows-amd64-licenses.txt", "browser_download_url": "https://example.com/lic"},
            {"name": "tunnel-client-v0.0.14-windows-amd64.zip", "browser_download_url": "https://example.com/winzip"},
            {"name": "tunnel-client_windows_amd64.exe", "browser_download_url": "https://example.com/win"},
            {"name": "notes.txt", "browser_download_url": "https://example.com/notes"},
        ]
        with patch.object(tunnel, "platform_asset_tokens", return_value=("windows", "win", "amd64")):
            chosen = tunnel.choose_release_asset(assets)
        self.assertTrue(str(chosen["name"]).endswith((".exe", ".zip")))
        self.assertNotIn("license", str(chosen["name"]))

    def test_chatgpt_steps_do_not_mention_public_port_exposure(self) -> None:
        text = tunnel.chatgpt_steps("tunnel_abc")
        self.assertIn("connection_status", text)
        self.assertIn("capabilities", text)
        self.assertIn("apply_patch", text)
        self.assertIn("read_file", text)
        self.assertIn("git", text)
        self.assertIn("logs", text)
        self.assertIn("Tunnel", text)
        self.assertNotIn("ngrok", text.lower())


if __name__ == "__main__":
    unittest.main()

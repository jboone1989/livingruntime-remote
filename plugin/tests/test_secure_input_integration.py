from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugin" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import bridge

WIDGET = ROOT / "relay" / "secure_input_widget.html"
MANAGER = ROOT / "relay" / "credential_manager_widget.html"
RELAY = ROOT / "relay" / "server.py"


class SecureInputIntegrationTests(unittest.TestCase):
    def test_connector_only_returns_public_data(self):
        with patch.object(bridge, "_config", return_value={}), \
             patch.object(bridge, "host_for", return_value={"id": "target"}), \
             patch.object(bridge, "resolve_path", return_value="/work/repo"), \
             patch.object(bridge.vault_client, "call",
                          return_value={"session_id": "secret_test", "server_public_key": "public",
                                        "handle": "cloud", "device": "target"}) as begin, \
             patch.object(bridge, "_audit") as audit:
            r = bridge.secure_input_open(
                handle="cloud", provider="cloudflare", capability="cloudflare.api",
                project="repo", device="target",
            )
        self.assertEqual(r["session_id"], "secret_test")
        self.assertEqual(begin.call_args.kwargs["device"], "target")
        self.assertEqual(begin.call_args.args, ("input_begin",))
        self.assertNotIn("server_public_key", str(audit.call_args))

    def test_write_accepts_only_ciphertext_and_returns_receipt(self):
        expected = {"stored": True, "handle": "cloud", "provider": "provider",
                    "capabilities": ["cap"], "projects": [], "devices": ["host"]}
        with patch.object(bridge.vault_client, "call", return_value=expected) as submit, \
             patch.object(bridge, "_audit") as audit:
            r = bridge.secure_input_submit("secret_x", "nonce64", "ciphertext64")
        self.assertEqual(r, expected)
        self.assertEqual(submit.call_args.args, ("input_submit",))
        self.assertEqual(submit.call_args.kwargs["ciphertext"], "ciphertext64")
        self.assertNotIn("ciphertext64", str(audit.call_args))

    def test_masked_widget_never_sends_plaintext(self):
        html = WIDGET.read_text(encoding="utf-8")
        source = RELAY.read_text(encoding="utf-8")
        self.assertIn('type="password"', html)
        self.assertIn('secure_input_submit', html)
        self.assertIn('crypto.subtle.encrypt', html)
        self.assertIn('secure_input_key_exchange', html)
        self.assertIn('visibility=["app"]', source)
        self.assertIn('SECURE_INPUT_WIDGET_URI', source)
        self.assertNotIn('innerHTML', html)
        self.assertNotIn('setWidgetState', html)
        self.assertNotIn('console.log', html)

    def test_manager_is_metadata_only_and_requires_deletion_confirmation(self):
        html = MANAGER.read_text(encoding="utf-8")
        source = RELAY.read_text(encoding="utf-8")
        self.assertIn('verify.value!==item.handle', html)
        self.assertIn('delete_credential', html)
        self.assertIn('revoke_credential_lease', html)
        self.assertIn('credential_manager', source)
        self.assertNotIn('innerHTML', html)
        self.assertNotIn('setWidgetState', html)
        if shutil.which("node"):
            script, = re.findall(r"<script>(.*?)</script>", html, re.S)
            compiled = subprocess.run(
                ["node", "--check"], input=script, text=True,
                capture_output=True, timeout=10,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_widget_javascript_parses(self):
        script, = re.findall(r"<script>(.*?)</script>",
                             WIDGET.read_text(encoding="utf-8"), re.S)
        completed = subprocess.run(["node", "--check"], input=script,
                                   text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()

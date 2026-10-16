"""Static browser security contract and JavaScript syntax checks."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIDGET = ROOT / "relay" / "secure_terminal_widget.html"
RELAY = ROOT / "relay" / "server.py"


class SecureTerminalWidgetTests(unittest.TestCase):
    def test_client_side_encryption_and_secret_free_model_boundary(self):
        html = WIDGET.read_text(encoding="utf-8")
        relay = RELAY.read_text(encoding="utf-8")
        self.assertIn('type="password"', html)
        self.assertIn('autocomplete="off"', html)
        self.assertIn('crypto.subtle.deriveKey', html)
        self.assertIn('crypto.subtle.encrypt', html)
        self.assertIn('crypto.subtle.decrypt', html)
        self.assertIn('terminal_input', html)
        self.assertNotIn('console.log', html)
        self.assertNotIn('setWidgetState', html)
        self.assertNotIn('innerHTML', html)
        self.assertIn('visibility=["app"]', relay)
        self.assertIn("secure_terminal_widget.html", relay)

    @unittest.skipUnless(shutil.which("node"), "node is not available")
    def test_browser_javascript_syntax(self):
        html = WIDGET.read_text(encoding="utf-8")
        script, = re.findall(r"<script>(.*?)</script>", html, re.S)
        result = subprocess.run(
            ["node", "--check"], input=script, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

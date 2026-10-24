"""The interactive install route should be one step and give useful failures."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import connector
import relay_agent


class ConnectorOnboardingTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Linux/macOS interactive setup")
    def test_linux_no_arg_setup_defaults_to_local_and_creates_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(connector.Path, "home", return_value=home), \
                 patch("builtins.input", side_effect=["ABCD-1234", ""]), \
                 patch.object(connector, "install", return_value={"autostart": "systemd"}) as install:
                connector.interactive_setup()
            install.assert_called_once()
            self.assertTrue(install.call_args.kwargs["local"])
            self.assertEqual(install.call_args.kwargs["host"], "local")
            self.assertEqual(install.call_args.kwargs["pair_code"], "ABCD-1234")
            self.assertEqual(install.call_args.kwargs["root"], str(home / "workspace"))
            self.assertTrue((home / "workspace").is_dir())

    @unittest.skipUnless(os.name == "posix", "Linux/macOS interactive setup")
    def test_custom_root_still_targets_local_machine(self):
        with patch("builtins.input", side_effect=["ABCD-1234", "/srv/projects"]), \
             patch.object(connector, "install", return_value={"autostart": "systemd"}) as install:
            connector.interactive_setup()
        self.assertTrue(install.call_args.kwargs["local"])
        self.assertEqual(install.call_args.kwargs["host"], "local")
        self.assertEqual(install.call_args.kwargs["root"], "/srv/projects")

    def test_http_401_pairing_error_contains_next_action(self):
        error = urllib.error.HTTPError(
            "https://remote.livingruntime.com/device/pair", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"pairing code is invalid or expired"}')
        )
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(relay_agent.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "Request a new code in ChatGPT"):
                relay_agent._request("https://remote.livingruntime.com", "/device/pair", {"code": "ABCD-1234"})

if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bridge


class SecureTerminalBridgeTests(unittest.TestCase):
    def test_unapproved_pty_cannot_launch_even_for_builtin(self):
        with patch.object(bridge, "_config", return_value={}), \
             patch.object(bridge, "host_for", return_value={"id": "test"}), \
             patch.object(bridge, "resolve_path", return_value="/home/ubuntu/project"), \
             patch.object(bridge, "dynamic_exec_grant", return_value=None) as granted, \
             patch.object(bridge, "ensure_dynamic_exec_request",
                          return_value={"request_id": "req_test", "risk": "interactive"}) as request, \
             patch.object(bridge, "_audit"), \
             patch.object(bridge.secure_terminal, "create") as create:
            result = bridge.terminal_start(["node", "script.js"], project="demo")
        self.assertTrue(result["approval_required"])
        self.assertEqual(result["request"]["request_id"], "req_test")
        self.assertTrue(granted.call_args.kwargs["detached"])
        self.assertTrue(request.call_args.kwargs["detached"])
        create.assert_not_called()

    def test_exact_grant_allows_only_bounded_session(self):
        with patch.object(bridge, "_config", return_value={}), \
             patch.object(bridge, "host_for", return_value={"id": "test"}), \
             patch.object(bridge, "resolve_path", return_value="/home/ubuntu/project"), \
             patch.object(bridge, "_transport", return_value="local"), \
             patch.object(bridge, "dynamic_exec_grant", return_value={"grant_id": "ok"}), \
             patch.object(bridge, "ensure_dynamic_exec_request") as request, \
             patch.object(bridge, "_audit") as audit, \
             patch.object(bridge.secure_terminal, "create",
                          return_value={"session_id": "term_test", "server_public_key": "public", "ttl_seconds": 300}) as create:
            result = bridge.terminal_start(["node", "script.js"], project="demo")
        self.assertEqual(result["session_id"], "term_test")
        request.assert_not_called()
        self.assertEqual(create.call_args.args, (["node", "script.js"], "/home/ubuntu/project", None))
        self.assertNotIn("server_public_key", str(audit.call_args))

    def test_inline_code_is_still_blocked(self):
        with patch.object(bridge.secure_terminal, "create") as create:
            with self.assertRaises(PermissionError):
                bridge.terminal_start(["python3", "-c", "print(123)"])
        create.assert_not_called()

    def test_encrypted_only_input_interface(self):
        with patch.object(bridge.secure_terminal, "send", return_value={"accepted": True, "seq": 1}) as write:
            result = bridge.terminal_input("term_test", 1, "base64nonce", "encryptedbytes")
        self.assertTrue(result["accepted"])
        self.assertEqual(write.call_args.args, ("term_test", 1, "base64nonce", "encryptedbytes"))


if __name__ == "__main__":
    unittest.main()

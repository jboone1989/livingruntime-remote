"""Operator-only Codex registration leaves Relay pairing and other agents intact."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import connector
import adapterctl
from livingruntime_adapter_codex import CodexAdapter


class CodexRegistrationTests(unittest.TestCase):
    def test_configure_existing_local_cli_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            workspace = home / "workspace"
            workspace.mkdir()
            codex = home / "codex"
            codex.write_text("#!/bin/sh\necho codex\n")
            codex.chmod(0o700)
            config = home / ".livingruntime" / "remote.json"
            config.parent.mkdir()
            config.write_text(json.dumps({
                "hosts": {"main": {"transport": "local"}},
                "projects": {"workspace": {"host": "main", "path": str(workspace)}},
            }))
            relay = config.parent / "relay.json"
            relay.write_text(json.dumps({"device_id": "unchanged-pair", "device_token": "do-not-touch"}))
            existing = config.parent / "agent-adapters.json"
            existing.write_text(json.dumps({"adapters": [{"module": "livingruntime_adapter_claude", "factory": "make"}]}))
            ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with patch.object(connector.Path, "home", return_value=home), \
                 patch.object(connector, "_run", return_value=ok) as run:
                first = connector.configure_codex(str(codex), "workspace")
                second = connector.configure_codex(str(codex), "workspace")
                self.assertTrue(first["configured"])
                self.assertTrue(second["restart_scheduled"])
                self.assertEqual(run.call_count, 2)
                policy = json.loads((config.parent / "agent-policy.json").read_text())
                self.assertEqual(policy["codex_executable"], str(codex))
                self.assertEqual(policy["claude_workspaces"]["main:workspace"], str(workspace))
                self.assertEqual(policy["launch_allowlist"], ["main:workspace:codex"])
                entries = json.loads(existing.read_text())["adapters"]
                self.assertEqual(len(entries), 2)
                self.assertEqual(entries[0]["module"], "livingruntime_adapter_claude")
                self.assertEqual(entries[1]["module"], "livingruntime_adapter_codex")
                self.assertEqual(json.loads(relay.read_text())["device_id"], "unchanged-pair")
                with patch.dict(os.environ, {"LIVINGRUNTIME_AGENT_POLICY": str(config.parent/"agent-policy.json"),
                                            "LIVINGRUNTIME_CODEX_JOBS": str(home/"jobs")}):
                    adapter = CodexAdapter()
                    self.assertEqual(adapter.executable, str(codex))

    def test_rejects_remote_host_project(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            codex = home / "codex"
            codex.write_text("placeholder")
            codex.chmod(0o700)
            config = home / ".livingruntime" / "remote.json"
            config.parent.mkdir()
            config.write_text(json.dumps({
                "hosts": {"main": {"transport": "ssh"}},
                "projects": {"workspace": {"host": "main", "path": str(home)}},
            }))
            with patch.object(connector.Path, "home", return_value=home):
                with self.assertRaisesRegex(ValueError, "local host"):
                    connector.configure_codex(str(codex), "workspace")


if __name__ == "__main__":
    unittest.main()

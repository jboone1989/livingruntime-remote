import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import agent_policy

class AgentPolicyTests(unittest.TestCase):
    def test_missing_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ,{"LIVINGRUNTIME_AGENT_POLICY":str(Path(root)/"absent"),
                                     "LIVINGRUNTIME_AGENT_LAUNCH_ALLOWLIST":"",
                                     "LIVINGRUNTIME_CLAUDE_WORKSPACES":"{}"}):
                self.assertFalse(agent_policy.may_launch("main","demo","claude-code"))
                with self.assertRaises(PermissionError):
                    agent_policy.claude_workspace("main","demo")
    def test_exact_scope_and_owner_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"policy.json"
            path.write_text(json.dumps({"launch_allowlist":["main:demo:claude-code"],
                "claude_workspaces":{"main:demo":root}}))
            with patch.dict(os.environ,{"LIVINGRUNTIME_AGENT_POLICY":str(path),
                                     "LIVINGRUNTIME_AGENT_LAUNCH_ALLOWLIST":"",
                                     "LIVINGRUNTIME_CLAUDE_WORKSPACES":"{}"}):
                with self.assertRaises(PermissionError):
                    agent_policy.may_launch("main","demo","claude-code")
                path.chmod(0o600)
                self.assertTrue(agent_policy.may_launch("main","demo","claude-code"))
                self.assertFalse(agent_policy.may_launch("other","demo","claude-code"))
                self.assertFalse(agent_policy.may_launch("main","other","claude-code"))
                self.assertEqual(agent_policy.claude_workspace("main","demo"),root)

if __name__=="__main__":
    unittest.main()

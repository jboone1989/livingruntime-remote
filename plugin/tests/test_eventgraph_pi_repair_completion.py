"""Native Pi repair lifecycle on existing Connector, without model/network."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eventgraph_pi_repair_completion as completion

SHA = "a" * 40
SESSION = "lra_" + "b" * 32
GOAL = f"[eventgraph-pi-repair pr=175 sha={SHA}] Fix this failing PR"


def task():
    return {"tool": "start_agent",
            "args": {"adapter_id": "pi", "project": "aihot-dev", "goal": GOAL}}


def launched():
    return {"ok": True, "result": {"session_id": SESSION}}


class RepairCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"LIVINGRUNTIME_PI_REPAIR_JOURNALS": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = patch.object(completion.agent_sessions, "get", return_value={
            "adapter_id": "pi", "project": "aihot-dev"})
        self.session.start()
        self.addCleanup(self.session.stop)
        completion.register(task(), launched())
        self.path = completion.journal_dir() / (SESSION + ".json")
        self.assertTrue(self.path.exists())

    def test_eligible_session_has_exact_worktree_and_no_tokens(self):
        state = json.loads(self.path.read_text())
        self.assertEqual(state["sha"], SHA)
        self.assertEqual(state["pr_number"], 175)
        self.assertTrue(state["worktree"].endswith(f"/pi-repair-175-{SHA[:12]}"))
        self.assertNotIn("device_token", str(state))
        completion.register(task(), launched())  # same session is idempotent

    def test_success_exports_and_delivers_at_most_once(self):
        observed = {"agent_status": {"status": "SUCCEEDED"}}
        sent = []
        def request(base, path, body, token, **kwargs):
            sent.append((path, body, token))
            return {"ok": True}
        with patch.object(completion.agent_sessions, "status", return_value=observed), \
             patch.object(completion, "export", return_value={"changes": [{"path": "x"}]}):
            self.assertTrue(completion.deliver({"name": "cn-dev", "url": "https://relay.test",
                                                "device_token": "ephemeral"}, request))
            self.assertFalse(completion.deliver({"name": "cn-dev", "url": "https://relay.test",
                                                 "device_token": "ephemeral"}, request))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "/device/pi-repair-result")
        self.assertEqual(sent[0][1]["session_id"], SESSION)
        self.assertEqual(json.loads(self.path.read_text())["state"], "DELIVERED")

    def test_network_failure_retains_durable_journal_and_retries(self):
        def failed(*a, **kw):
            raise OSError("unreachable")
        with patch.object(completion.agent_sessions, "status",
                          return_value={"agent_status": {"status": "SUCCEEDED"}}), \
             patch.object(completion, "export", return_value={"changes": []}):
            self.assertFalse(completion.deliver({"name": "cn-dev"}, failed))
            self.assertEqual(json.loads(self.path.read_text())["state"], "RUNNING")

    def test_failed_native_session_does_not_send_or_restart(self):
        called = []
        with patch.object(completion.agent_sessions, "status",
                          return_value={"agent_status": {"status": "FAILED"}}):
            self.assertFalse(completion.deliver({"name": "cn-dev"}, lambda *args: called.append(args)))
        self.assertEqual(called, [])
        self.assertEqual(json.loads(self.path.read_text())["state"], "BLOCKED")

    def test_other_connector_must_not_forward_repair(self):
        self.assertFalse(completion.deliver({"name": "owner-main"}, lambda *a: self.fail("not allowed")))
        self.assertEqual(json.loads(self.path.read_text())["state"], "RUNNING")

    def test_unmarked_normal_agent_session_is_ignored(self):
        with patch.object(completion.agent_sessions, "get") as get:
            completion.register({**task(), "args": {**task()["args"], "goal": "Normal Pi work"}},
                                launched())
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()

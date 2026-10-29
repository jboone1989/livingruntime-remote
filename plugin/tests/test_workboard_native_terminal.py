"""Workboard native Pi terminal lifecycle without Relay or model access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from workboard_worker import Worker

PLACEMENT = {"agent_id": "fixer", "adapter_id": "pi", "project": "aihot-dev",
             "local_device": "cn-dev"}
ITEM = {"id": "lrwork_abc", "fence": 1, "agent_id": "fixer",
        "adapter_id": "pi", "project": "aihot-dev", "local_device": "cn-dev",
        "goal": "Fix an approved bug", "acceptance_json": "[]",
        "execution_done": 0, "session_id": None, "runtime_job_id": None}
SESSION = {"session_id": "lra_abc", "runtime_job_id": "lrjob_abc"}


class WorkboardNativeTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"LIVINGRUNTIME_WORKBOARD_JOURNALS": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.launches = []
        self.claimed = False
        self.native = "RUNNING"
        self.state = "CLAIMED"

        def call(url, path, payload, token, timeout=30):
            self.assertEqual(path, "/device/workboard")
            operation = payload["operation"]
            self.calls.append((operation, dict(payload)))
            if operation == "claim":
                if self.claimed:
                    return {"item": None}
                self.claimed = True
                return {"item": dict(ITEM)}
            if operation == "heartbeat":
                return {"item": {**ITEM, "status": self.state}}
            if operation == "started":
                self.state = "RUNNING"
            if operation == "blocked":
                self.state = "BLOCKED_HUMAN"
            return {"item": {**ITEM, "status": self.state}}

        def start(adapter_id, goal, *, project, device):
            self.launches.append((adapter_id, goal, project, device))
            return SESSION

        self.worker = Worker({"url": "https://relay.invalid", "device_id": "device",
                              "device_token": "secret"}, PLACEMENT, call,
                             start=start,
                             status=lambda session_id: {
                                 "agent_status": {"status": self.native}})
        self.worker.tick()  # native session launch + persisted journal
        self.assertTrue(self.worker.path.exists())
        self.assertEqual(len(self.launches), 1)

    def test_stalled_pi_is_blocked_once_and_never_relaunched(self):
        self.native = "STALLED"
        self.worker.tick()
        blocked = [x for x in self.calls if x[0] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("Native STALLED", blocked[0][1]["reason"])
        self.assertEqual(self.state, "BLOCKED_HUMAN")
        self.assertFalse(self.worker.path.exists())
        self.assertEqual(len(list(Path(self.temp.name).glob("*.receipt.json"))), 1)
        self.worker.tick()
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len([x for x in self.calls if x[0] == "blocked"]), 1)

    def test_running_agent_keeps_lease_without_false_completion(self):
        self.worker.tick()
        self.assertTrue(self.worker.path.exists())
        self.assertFalse(any(x[0] in {"blocked", "finished"} for x in self.calls))
        self.assertEqual(self.state, "RUNNING")
        self.assertEqual(len(self.launches), 1)

    def enable_acceptance(self):
        current = json.loads(self.worker.path.read_text())
        current['item']['acceptance_json'] = json.dumps(['node', '--version'])
        self.worker.save(current)
        self.native = 'SUCCEEDED'

    def test_transient_acceptance_transport_failure_retries_without_relaunch(self):
        self.enable_acceptance()
        attempts = []

        def check(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise ConnectionError('temporary Relay outage')
            return {'returncode': 0}

        self.worker.check = check
        self.worker.tick()
        self.assertTrue(self.worker.path.exists())
        self.assertEqual(json.loads(self.worker.path.read_text())['verification_failures'], 1)
        self.assertFalse(any(op == 'blocked' for op, _ in self.calls))
        self.worker.tick()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len([op for op, _ in self.calls if op == 'finished']), 1)
        self.assertFalse(self.worker.path.exists())

    def test_persistent_acceptance_transport_failure_blocks_and_handoffs(self):
        self.enable_acceptance()
        attempts = []

        def unavailable(**kwargs):
            attempts.append(kwargs)
            raise ConnectionError('Relay unavailable')

        self.worker.check = unavailable
        for attempt in range(3):
            self.worker.tick()
            if attempt < 2:
                self.assertTrue(self.worker.path.exists())
        blocked = [fields for op, fields in self.calls if op == 'blocked']
        self.assertEqual(len(blocked), 1)
        self.assertTrue(blocked[0]['execution_done'])
        self.assertIn('Acceptance check unavailable after 3 attempts', blocked[0]['reason'])
        self.assertEqual(self.state, 'BLOCKED_HUMAN')
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.launches), 1)
        self.assertFalse(self.worker.path.exists())
        self.assertEqual(len(list(Path(self.temp.name).glob('*.receipt.json'))), 1)


if __name__ == "__main__":
    unittest.main()

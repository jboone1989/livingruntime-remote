from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cognition  # noqa: E402


class CognitionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cognition"
        self.env = patch.dict(
            os.environ,
            {"LIVINGRUNTIME_COGNITION_ROOT": str(self.root)},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def submit(self, request_id: str | None = None, *, agent_id: str = "ferro"):
        return cognition.submit(
            agent_id=agent_id,
            purpose="strategy_planning",
            messages=[
                {"role": "system", "content": "Reason carefully."},
                {"role": "user", "content": "Choose the next action."},
            ],
            response_format={"type": "text"},
            options={"max_output_tokens": 800},
            metadata={"goal_id": "goal-1"},
            timeout_seconds=300,
            request_id=request_id,
        )

    def test_submit_is_durable_private_and_idempotent(self) -> None:
        first = self.submit("llmreq_fixed")
        second = self.submit("llmreq_fixed")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(first["status"], "PENDING")
        path = self.root / "requests" / "llmreq_fixed.json"
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_request_id_conflict_is_rejected(self) -> None:
        self.submit("llmreq_conflict")
        with self.assertRaisesRegex(RuntimeError, "idempotency conflict"):
            cognition.submit(
                agent_id="ferro",
                purpose="different",
                messages=[{"role": "user", "content": "different"}],
                request_id="llmreq_conflict",
            )

    def test_claim_serializes_agent_and_completion_records_provenance(self) -> None:
        first = self.submit("llmreq_one")
        self.submit("llmreq_two")

        claimed = cognition.claim_next(agent_id="ferro", watcher_id="watcher_1")
        self.assertEqual(claimed["request_id"], first["request_id"])
        self.assertEqual(claimed["status"], "DISPATCHED")
        token = claimed["claim"]["token"]

        self.assertIsNone(cognition.claim_next(agent_id="ferro", watcher_id="watcher_2"))

        with self.assertRaisesRegex(RuntimeError, "claim token mismatch"):
            cognition.complete(
                request_id=first["request_id"],
                response_text="answer",
                claim_token="wrong",
            )

        done = cognition.complete(
            request_id=first["request_id"],
            response_text="answer",
            claim_token=token,
            model="gpt-5.6-sol",
            session_id="session-test",
        )
        self.assertEqual(done["status"], "COMPLETED")
        self.assertEqual(done["response"]["provider"], "livingruntime-chatgpt")
        self.assertEqual(done["response"]["model"], "gpt-5.6-sol")
        self.assertEqual(done["response"]["session_id"], "session-test")
        self.assertEqual(
            done["response"]["sha256"],
            hashlib.sha256(b"answer").hexdigest(),
        )

        second = cognition.claim_next(agent_id="ferro", watcher_id="watcher_2")
        self.assertEqual(second["request_id"], "llmreq_two")

    def test_stale_claim_can_be_reclaimed(self) -> None:
        self.submit("llmreq_reclaim")
        with patch.object(cognition.time, "time", return_value=1000.0):
            claimed = cognition.claim_next(
                agent_id="ferro",
                watcher_id="watcher_old",
                claim_seconds=15,
            )
        old_token = claimed["claim"]["token"]

        with patch.object(cognition.time, "time", return_value=1020.0):
            reclaimed = cognition.claim_next(
                agent_id="ferro",
                watcher_id="watcher_new",
                claim_seconds=15,
            )
        self.assertEqual(reclaimed["request_id"], "llmreq_reclaim")
        self.assertNotEqual(reclaimed["claim"]["token"], old_token)
        self.assertEqual(reclaimed["claim"]["watcher_id"], "watcher_new")
        self.assertEqual(reclaimed["attempts"], 2)

    def test_deadline_moves_request_to_timed_out(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            created = cognition.submit(
                agent_id="ferro",
                purpose="timeout",
                messages=[{"role": "user", "content": "hello"}],
                timeout_seconds=5,
                request_id="llmreq_timeout",
            )
        self.assertEqual(created["deadline_at"], 1005.0)
        with patch.object(cognition.time, "time", return_value=1006.0):
            timed_out = cognition.get("llmreq_timeout")
        self.assertEqual(timed_out["status"], "TIMED_OUT")
        self.assertEqual(timed_out["finished_at"], 1006.0)

    def test_agent_queues_are_isolated(self) -> None:
        self.submit("llmreq_ferro", agent_id="ferro")
        self.submit("llmreq_other", agent_id="other-agent")
        claimed = cognition.claim_next(agent_id="other-agent", watcher_id="watcher_other")
        self.assertEqual(claimed["request_id"], "llmreq_other")
        ferro = cognition.get("llmreq_ferro")
        self.assertEqual(ferro["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()

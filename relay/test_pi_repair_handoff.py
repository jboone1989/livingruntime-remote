"""Durable Pi repair handoff: owner identity, exact SHA, replay and ACK."""
import json
from pathlib import Path
import tempfile
import unittest

from store import RelayStore
from pi_repair_handoff import RepairHandoff

SHA = "a" * 40
NEXT = "b" * 40
CN = "e21c871f-317b-4f3e-95bf-8d4299547a40"
SESSION = "lra_" + "c" * 32


def patch():
    return {"repository": "jboone1989/EventGraph", "pr_number": 175,
            "expected_head_sha": SHA,
            "changes": [{"path": "packages/backend/src/editorial/explain.ts",
                         "expected_blob_sha": "d" * 40, "content": "export const fixed = true;\n"}]}


class PiRepairHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RelayStore(str(Path(self.tmp.name) / "relay.sqlite3"))
        self.board = RepairHandoff(self.store)
        self.board.record("owner", CN, 175, SHA, SESSION)

    def test_reservation_precedes_launch_and_allows_exactly_one_session(self):
        reservation = self.board.reserve("owner", CN, 177, NEXT)
        with self.assertRaisesRegex(PermissionError, "already claimed"):
            self.board.reserve("owner", CN, 177, NEXT)
        with self.assertRaisesRegex(PermissionError, "missing or consumed"):
            self.board.activate("owner", "unpaired-device", reservation, "lra_" + "d" * 32)
        actual = "lra_" + "d" * 32
        self.board.activate("owner", CN, reservation, actual)
        with self.assertRaisesRegex(PermissionError, "missing or consumed"):
            self.board.activate("owner", CN, reservation, "lra_" + "e" * 32)
        source = {**patch(), "pr_number": 177, "expected_head_sha": NEXT}
        self.board.submit("owner", CN, actual, source)
        self.assertEqual(self.board.pending("owner")[0]["session_id"], actual)

    def test_ambiguous_launch_leaves_durable_claim_instead_of_second_agent(self):
        reservation = self.board.reserve("owner", CN, 177, NEXT)
        with self.store.db() as db:
            row = db.execute("SELECT state FROM pi_repair_handoffs WHERE session_id=?",
                             (reservation,)).fetchone()
        self.assertEqual(row["state"], "LAUNCHING")
        with self.assertRaises(PermissionError):
            self.board.reserve("owner", CN, 177, NEXT)
        self.assertEqual(self.board.pending("owner"), [])

    def test_exact_owned_session_and_immutable_head(self):
        self.board.submit("owner", CN, SESSION, patch())
        result = self.board.pending("owner")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["manifest"], patch())
        self.assertEqual(self.board.pending("other"), [])
        with self.assertRaises(PermissionError):
            self.board.submit("other", CN, SESSION, patch())
        with self.assertRaises(PermissionError):
            self.board.submit("owner", "another-device", SESSION, patch())
        with self.assertRaises(PermissionError):
            self.board.submit("owner", CN, SESSION, {**patch(), "expected_head_sha": NEXT})

    def test_identical_retry_idempotent_but_conflicting_result_denied(self):
        self.board.submit("owner", CN, SESSION, patch())
        self.board.submit("owner", CN, SESSION, patch())
        changed = patch()
        changed["changes"][0]["content"] = "different"
        with self.assertRaises(PermissionError):
            self.board.submit("owner", CN, SESSION, changed)

    def test_ack_only_once_and_owner_scoped(self):
        self.board.submit("owner", CN, SESSION, patch())
        with self.assertRaises(PermissionError):
            self.board.acknowledge("other", SESSION, SHA, NEXT)
        with self.assertRaises(ValueError):
            self.board.acknowledge("owner", SESSION, SHA, SHA)
        self.board.acknowledge("owner", SESSION, SHA, NEXT)
        self.assertEqual(self.board.pending("owner"), [])
        with self.assertRaises(PermissionError):
            self.board.acknowledge("owner", SESSION, SHA, NEXT)
        with self.assertRaises(PermissionError):
            self.board.submit("owner", CN, SESSION, patch())

    def test_new_attempt_cannot_displace_original_head(self):
        with self.assertRaises(PermissionError):
            self.board.record("owner", CN, 175, SHA, "lra_" + "e" * 32)
        with self.assertRaises(PermissionError):
            self.board.record("owner", "wrong-device", 175, SHA, SESSION)

    def test_payload_limit_and_shape(self):
        for invalid in [None, {}, {"repository": "fork/repo", **{
            k: v for k, v in patch().items() if k != "repository"
        }}, {**patch(), "changes": [{"content": "X" * 240_000}]}]:
            with self.subTest(invalid=str(invalid)[:80]), self.assertRaises(ValueError):
                self.board.submit("owner", CN, SESSION, invalid)
        self.assertEqual(self.board.pending("owner"), [])


if __name__ == "__main__":
    unittest.main()

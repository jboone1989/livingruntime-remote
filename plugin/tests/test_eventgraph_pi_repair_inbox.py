"""Trusted central Pi patch inbox consumer tests, no GitHub calls."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from eventgraph_pi_repair_inbox import drain

SHA = "a" * 40
NEXT = "b" * 40
MANIFEST = {"repository": "jboone1989/EventGraph", "pr_number": 175,
            "expected_head_sha": SHA, "changes": []}
ITEM = {"session_id": "lra_test", "pr_number": 175,
        "original_sha": SHA, "manifest": MANIFEST}


class InboxTests(unittest.TestCase):
    def test_patch_written_once_and_acknowledged_with_matching_sha(self):
        requests, writes = [], []
        def request(base, path, body, token, **kwargs):
            requests.append((path, body, token))
            return {"items": [ITEM]} if body["action"] == "pending" else {"ok": True}
        def writer(manifest):
            writes.append(manifest)
            return {"pr_number": 175, "previous_sha": SHA, "new_sha": NEXT}
        result = drain({"name": "owner-main", "url": "https://relay.invalid",
                        "device_token": "private"}, request, writer)
        self.assertTrue(result)
        self.assertEqual(writes, [MANIFEST])
        self.assertEqual(requests[1][1],
                         {"action": "ack", "session_id": "lra_test",
                          "original_sha": SHA, "new_sha": NEXT})

    def test_untrusted_connector_cannot_consume_inbox(self):
        self.assertFalse(drain({"name": "cn-dev"}, lambda *a: self.fail("read forbidden"),
                               lambda *a: self.fail("write forbidden")))

    def test_bad_envelope_does_not_call_writer_or_ack(self):
        for item in [{**ITEM, "original_sha": NEXT}, {**ITEM, "pr_number": 99},
                     {**ITEM, "extra": 1}]:
            paths = []
            def request(base, path, body, token, **kwargs):
                paths.append((path, body))
                return {"items": [item]}
            self.assertFalse(drain({"name": "owner-main", "url": "https://relay.invalid",
                                    "device_token": "token"}, request,
                                   lambda *a: self.fail("invalid item must not write")))
            self.assertEqual(len(paths), 1)

    def test_failure_keeps_inbox_item_pending_for_exact_commit_reconciliation(self):
        calls = []
        def request(base, path, body, token, **kwargs):
            calls.append(body["action"])
            return {"items": [ITEM]} if body["action"] == "pending" else {"ok": False}
        self.assertFalse(drain({"name": "owner-main", "url": "https://relay.invalid",
                                "device_token": "token"}, request,
                               lambda *a: {"pr_number": 175,
                                           "previous_sha": SHA, "new_sha": NEXT}))
        self.assertEqual(calls, ["pending", "ack"])

    def test_oversized_batch_rejected(self):
        with self.assertRaisesRegex(ValueError, "bounded"):
            drain({"name": "owner-main", "url": "https://relay.invalid",
                   "device_token": "token"}, lambda *a, **k: {"items": [ITEM] * 6})


if __name__ == "__main__":
    unittest.main()

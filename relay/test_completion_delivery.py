import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from store import RelayStore


class CompletionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RelayStore(str(Path(self.tmp.name) / "relay.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_result_upload_is_idempotent_after_consumption(self):
        code = self.store.create_pairing_code("user")
        device = self.store.pair_device(code["code"], "host")["device_id"]
        task = self.store.enqueue("user", device, "get_long_job", {})
        self.store.claim(device)
        self.store.complete(device, task, {"ok": True, "result": {"x": 1}})
        self.store.result("user", task, consume=True)
        self.store.complete(device, task, {"result": {"x": 1}, "ok": True})
        with self.assertRaises(KeyError):
            self.store.complete("other-device", task, {"ok": True, "result": {"x": 1}})
        with self.assertRaises(KeyError):
            self.store.complete(device, task, {"ok": True, "result": {"x": 2}})

    def test_concurrent_watchers_get_one_claim_and_acceptance_survives_reload(self):
        event = self.store.ensure_completion("user", "job")["event_id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: self.store.claim_completion("user", event), range(4)))
        winners = [c for c in claims if c.get("claim_token")]
        self.assertEqual(len(winners), 1)
        self.store.settle_completion("user", event, winners[0]["claim_token"], "accepted")
        restored = RelayStore(self.store.path)
        self.assertEqual(restored.ensure_completion("user", "job")["state"], "accepted")
        self.assertNotIn("claim_token", restored.claim_completion("user", event))

    def test_uncertain_delivery_requires_explicit_retry_and_rejects_stale_claim(self):
        event = self.store.ensure_completion("user", "job")["event_id"]
        old = self.store.claim_completion("user", event)["claim_token"]
        with self.store.db() as db:
            db.execute("UPDATE completion_events SET lease_until=0")
        self.assertEqual(self.store.claim_completion("user", event)["state"], "uncertain")
        new = self.store.claim_completion("user", event, retry_uncertain=True)["claim_token"]
        with self.assertRaises(PermissionError):
            self.store.settle_completion("user", event, old, "accepted")
        self.store.settle_completion("user", event, new, "pending")
        self.assertIn("claim_token", self.store.claim_completion("user", event))

    def test_user_scope_and_model_observation_win_over_late_delivery_error(self):
        event = self.store.ensure_completion("user", "job")["event_id"]
        claim = self.store.claim_completion("user", event)
        with self.assertRaises(KeyError):
            self.store.claim_completion("other", event)
        with self.assertRaises(KeyError):
            self.store.observe_completion("other", event)
        self.store.observe_completion("user", event)
        self.assertEqual(self.store.settle_completion("user", event, claim["claim_token"], "pending")["state"], "observed")


if __name__ == "__main__":
    unittest.main()

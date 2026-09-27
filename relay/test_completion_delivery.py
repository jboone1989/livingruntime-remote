import asyncio
import json
from unittest.mock import patch
from test_server import server
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

    def test_offer_crash_before_local_prepare_can_be_reoffered_and_ack_is_idempotent(self):
        task_id = self.store.enqueue("user", "device", "exec", {})
        first = self.store.offer("device")
        restored = RelayStore(self.store.path)
        self.assertEqual(restored.offer("device"), first)
        self.assertTrue(restored.accept_offer("device", task_id, "preparation-1"))
        self.assertTrue(restored.accept_offer("device", task_id, "preparation-1"))
        self.assertFalse(restored.accept_offer("device", task_id, "different-connector"))
        self.assertFalse(restored.accept_offer("other", task_id, "preparation-1"))
        self.assertIsNone(restored.offer("device"))
        self.assertFalse(restored.cancel_if_queued("user", task_id))
        cancelled = restored.enqueue("user", "device", "exec", {})
        restored.offer("device")
        self.assertTrue(restored.cancel_if_queued("user", cancelled))
        self.assertFalse(restored.accept_offer("device", cancelled, "late-preparation"))

    def test_late_result_is_recoverable_after_call_timeout_without_redispatch(self):
        device = self.store.pair_device(self.store.create_pairing_code("user")["code"], "test")["device_id"]
        relay = server.Relay(self.store, reconnect_grace=0)
        async def timeout_after_claim(awaitable, timeout):
            awaitable.close()
            self.store.claim(device)
            raise TimeoutError
        with patch.object(server.asyncio, "wait_for", side_effect=timeout_after_claim):
            pending = asyncio.run(relay.call("user", "start_long_job", {"command": "side effect"}))
        self.assertEqual(pending["status"], "DELIVERY_UNCERTAIN")
        task_id = pending["relay_task_id"]
        self.assertEqual(relay.task_receipt("user", task_id)["status"], "CLAIMED")
        result = {"ok": True, "result": {"job": {"job_id": "job-1"}, "terminal": True}}
        self.store.complete(device, task_id, result)
        recovered = server.Relay(RelayStore(self.store.path)).task_receipt("user", task_id)
        self.assertEqual(recovered["result"]["job"]["job_id"], "job-1")
        self.assertEqual(relay.task_receipt("user", task_id), recovered)
        self.assertIsNone(self.store.claim(device))
        with self.assertRaises(KeyError):
            relay.task_receipt("other", task_id)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_pi_completion_identity_does_not_depend_on_observer_path(self):
        relay = server.Relay(self.store)
        ids = []
        for tool, args in [
            ("start_pi_agent", {}),
            ("watch_pi_job", {"job_id": "pi-1", "pi_remote_dir": "/custom/pi/", "job_root": "/jobs/"}),
            ("wait_pi_job_completion", {"job_id": "pi-1", "pi_remote_dir": "/custom/pi", "job_root": "/jobs"}),
        ]:
            value = relay._decorate_result("user", "device", tool, args,
                {"terminal": True, "jobId": "pi-1", "runtimeJobId": "optional-runtime"}, "task")
            ids.append(value["completionDelivery"]["event_id"])
        self.assertEqual(len(set(ids)), 1)

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

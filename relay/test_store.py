from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from store import RelayStore, digest


class RelayStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RelayStore(str(Path(self.tmp.name) / "relay.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def pair(self, user: str, name: str):
        code = self.store.create_pairing_code(user)
        return self.store.pair_device(code["code"], name)

    def test_pairing_is_one_time_and_device_token_is_hashed(self):
        created = self.store.create_pairing_code("user-a")
        paired = self.store.pair_device(created["code"], "laptop")
        with self.assertRaises(PermissionError):
            self.store.pair_device(created["code"], "second")
        with self.store.db() as db:
            row = db.execute("SELECT token_hash FROM devices WHERE device_id=?",
                             (paired["device_id"],)).fetchone()
        self.assertEqual(row["token_hash"], digest(paired["device_token"]))
        self.assertNotEqual(row["token_hash"], paired["device_token"])

    def test_tasks_are_isolated_by_user_and_device(self):
        a = self.pair("user-a", "a")
        b = self.pair("user-b", "b")
        task = self.store.enqueue("user-a", a["device_id"], "connection_status", {})
        self.assertIsNone(self.store.claim(b["device_id"]))
        claimed = self.store.claim(a["device_id"])
        self.assertEqual(claimed["task_id"], task)
        with self.assertRaises(KeyError):
            self.store.complete(b["device_id"], task, {"ok": True, "result": {}})
        self.store.complete(a["device_id"], task, {"ok": True, "result": {"ok": True}})
        with self.assertRaises(KeyError):
            self.store.result("user-b", task)
        self.assertEqual(self.store.result("user-a", task)["result"]["ok"], True)

    def test_device_authentication_rejects_unknown_token(self):
        paired = self.pair("user-a", "a")
        self.assertEqual(
            self.store.authenticate_device(paired["device_token"])["user_sub"], "user-a"
        )
        with self.assertRaises(PermissionError):
            self.store.authenticate_device("not-a-token")

    def test_cleanup_removes_stale_task_payloads(self):
        paired = self.pair("user-a", "a")
        task = self.store.enqueue("user-a", paired["device_id"], "connection_status", {})
        with self.store.db() as db:
            db.execute("UPDATE tasks SET created_at=0 WHERE task_id=?", (task,))
        self.store.cleanup(retention_seconds=1)
        with self.assertRaises(KeyError):
            self.store.result("user-a", task)

    def test_timeout_cancellation_only_removes_unclaimed_tasks(self):
        paired = self.pair("user-a", "a")
        queued = self.store.enqueue("user-a", paired["device_id"], "connection_status", {})
        self.assertTrue(self.store.cancel_if_queued("user-a", queued))
        with self.assertRaises(KeyError):
            self.store.result("user-a", queued)

        claimed = self.store.enqueue("user-a", paired["device_id"], "connection_status", {})
        self.store.claim(paired["device_id"])
        self.assertFalse(self.store.cancel_if_queued("user-a", claimed))
        self.store.complete(
            paired["device_id"], claimed, {"ok": True, "result": {"ok": True}}
        )
        self.assertTrue(self.store.result("user-a", claimed)["ok"])

    def test_result_can_be_consumed_and_device_revoked(self):
        paired = self.pair("user-a", "a")
        task = self.store.enqueue("user-a", paired["device_id"], "connection_status", {})
        self.store.claim(paired["device_id"])
        self.store.complete(paired["device_id"], task, {"ok": True, "result": {"x": 1}})
        self.assertEqual(self.store.result("user-a", task, consume=True)["result"]["x"], 1)
        with self.assertRaises(KeyError):
            self.store.result("user-a", task)
        self.assertTrue(self.store.revoke_device("user-a", paired["device_id"]))
        with self.assertRaises(PermissionError):
            self.store.authenticate_device(paired["device_token"])


if __name__ == "__main__":
    unittest.main()

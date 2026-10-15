import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import permissions

class PermissionMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_PERMISSIONS": str(Path(self.tmp.name) / "permissions.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def req(self, argv):
        return permissions.ensure_request(host_id="main", project="remote", cwd="/repo", argv=argv)

    def test_identical_pending_deduplicated(self):
        self.assertEqual(self.req(["git", "status"])["request_id"], self.req(["git", "status"])["request_id"])
        self.assertEqual(permissions.snapshot()["counts"]["pending"], 1)

    def test_expire_and_regenerate(self):
        with patch.object(permissions.time, "time", return_value=100.0):
            original = self.req(["git", "status"])
        with patch.object(permissions.time, "time", return_value=100.0 + permissions.PENDING_TTL_SECONDS + 1):
            current = self.req(["git", "status"])
            self.assertNotEqual(original["request_id"], current["request_id"])
            self.assertEqual(permissions.snapshot()["counts"]["pending"], 1)

    def test_approved_request_not_pending(self):
        request = self.req(["git", "status"])
        permissions.approve(request["request_id"])
        self.assertEqual(permissions.snapshot()["counts"]["pending"], 0)

    def test_listing_bounded_and_counted(self):
        for i in range(70):
            self.req(["python3", "-c", str(i)])
        snap = permissions.snapshot()
        self.assertEqual(snap["counts"]["pending"], 70)
        self.assertEqual(len(snap["pending"]), permissions.SNAPSHOT_LIMIT)
        self.assertTrue(snap["truncated"])

    def test_security_deny_unchanged(self):
        with self.assertRaises(PermissionError):
            self.req(["sudo", "true"])


    def test_expired_request_cannot_be_approved(self):
        with patch.object(permissions.time, "time", return_value=100.0):
            request = self.req(["git", "status"])
        with patch.object(permissions.time, "time", return_value=100.0 + permissions.PENDING_TTL_SECONDS + 1):
            with self.assertRaisesRegex(RuntimeError, "no longer pending"):
                permissions.approve(request["request_id"])

    def test_approval_is_idempotent_only_while_grant_active(self):
        request = self.req(["python3", "-m", "unittest"])
        a = permissions.approve(request["request_id"])
        b = permissions.approve(request["request_id"])
        self.assertEqual(a["permission_id"], b["permission_id"])
        permissions.revoke(a["permission_id"])
        with self.assertRaisesRegex(RuntimeError, "no longer pending"):
            permissions.approve(request["request_id"])


    def test_large_realistic_store_snapshot_is_bounded(self):
        grants = [{"permission_id": "grant_" + str(i), "revoked_at": None,
                   "host_id": "main", "project": "remote", "cwd": "/repo",
                   "argv": ["python3", "-m", "unittest", str(i)],
                   "detached": False} for i in range(2250)]
        requests = [{"request_id": "req_" + str(i), "status": "APPROVED",
                     "created_at": 1, "argv": ["python3", str(i)]}
                    for i in range(2500)]
        requests.extend([{"request_id": "pending_" + str(i),
                          "status": "PENDING", "created_at": time.time(),
                          "argv": ["python3", str(i)]} for i in range(59)])
        permissions._save({"version": 1, "grants": grants, "requests": requests})
        result = permissions.snapshot()
        self.assertEqual(result["counts"]["grants"], 2250)
        self.assertEqual(result["counts"]["pending"], 59)
        self.assertEqual(len(result["grants"]), 50)
        self.assertEqual(len(result["pending"]), 50)
        self.assertTrue(result["truncated"])
        full = permissions._load()
        self.assertEqual(len(full["grants"]), 2250)
        self.assertEqual(len(full["requests"]), 559)

    def test_response_stays_below_relay_budget_with_verbose_argv(self):
        for i in range(50):
            self.req(["python3", "-c", "x" * 3500 + str(i)])
        result = permissions.snapshot()
        import json
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 40000)
        self.assertEqual(result["counts"]["pending"], 50)
        self.assertTrue(result["truncated"])

if __name__ == "__main__":
    unittest.main()

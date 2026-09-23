from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import credentials  # noqa: E402


class CredentialBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "credentials"
        self.env = patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_CREDENTIALS": str(self.root)},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_secret_never_appears_in_public_metadata(self) -> None:
        secret = "example-sensitive-value"
        row = credentials.set_local_secret(
            "github.production",
            secret,
            provider="github",
            capabilities=["git_push"],
            projects=["livingruntime-remote"],
            devices=["main"],
        )
        self.assertNotIn(secret, str(row))
        listed = credentials.list_handles()
        self.assertNotIn(secret, str(listed))
        self.assertEqual(listed[0]["handle"], "github.production")
        self.assertTrue(listed[0]["secret_present"])
        self.assertTrue(listed[0]["secret_permissions_ok"])

    def test_lease_is_capability_project_and_device_scoped(self) -> None:
        credentials.set_local_secret(
            "github.production",
            "sensitive",
            provider="github",
            capabilities=["git_push", "git_fetch"],
            projects=["livingruntime-remote"],
            devices=["main"],
        )
        lease = credentials.create_lease(
            "github.production",
            capability="git_push",
            project="livingruntime-remote",
            device="main",
            ttl_seconds=60,
        )
        self.assertTrue(lease["lease_id"].startswith("credlease_"))
        self.assertNotIn("sensitive", str(lease))
        with self.assertRaises(PermissionError):
            credentials.create_lease(
                "github.production",
                capability="email_send",
                project="livingruntime-remote",
                device="main",
            )
        with self.assertRaises(PermissionError):
            credentials.create_lease(
                "github.production",
                capability="git_push",
                project="ferro",
                device="main",
            )

    def test_internal_resolution_requires_exact_active_lease_scope(self) -> None:
        credentials.set_local_secret(
            "provider.primary",
            "hidden-value",
            provider="example",
            capabilities=["provider_call"],
            projects=["ferro"],
            devices=["main"],
        )
        lease = credentials.create_lease(
            "provider.primary",
            capability="provider_call",
            project="ferro",
            device="main",
            ttl_seconds=60,
        )
        resolved = credentials.resolve_secret_for_lease(
            lease["lease_id"],
            capability="provider_call",
            project="ferro",
            device="main",
        )
        self.assertEqual(resolved, "hidden-value")
        with self.assertRaises(PermissionError):
            credentials.resolve_secret_for_lease(
                lease["lease_id"],
                capability="provider_call",
                project="ferro",
                device="vultr",
            )
        credentials.revoke_lease(lease["lease_id"])
        with self.assertRaises(PermissionError):
            credentials.resolve_secret_for_lease(
                lease["lease_id"],
                capability="provider_call",
                project="ferro",
                device="main",
            )

    def test_remove_revokes_active_leases(self) -> None:
        credentials.set_local_secret(
            "mail.operator",
            "hidden",
            provider="mail",
            capabilities=["mail_send"],
        )
        lease = credentials.create_lease(
            "mail.operator",
            capability="mail_send",
            ttl_seconds=60,
        )
        credentials.remove_local_secret("mail.operator")
        rows = credentials.list_leases(active_only=False)
        found = next(row for row in rows if row["lease_id"] == lease["lease_id"])
        self.assertFalse(found["active"])
        self.assertFalse((self.root / "mail.operator.secret").exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import os
import secrets
import socket
import tempfile
import threading
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import credentials
import vault_client
import vault_service

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux Unix socket / SO_PEERCRED test")
class VaultIpcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = str(Path(self.tmp.name) / "vault.sock")
        self.store = Path(self.tmp.name) / "secret-store"
        self.store.mkdir(mode=0o700)
        self.patches = patch.dict(os.environ, {
            "LIVINGRUNTIME_VAULT_SOCKET": self.socket,
            "LIVINGRUNTIME_REMOTE_CREDENTIALS": str(self.store),
        })
        self.patches.start()
        self.server = vault_service.VaultServer(self.socket, os.getuid())
        os.chmod(self.socket, 0o600)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=1)
        self.patches.stop()
        self.tmp.cleanup()

    def test_unavailable_vault_refuses_plaintext_fallback(self):
        os.environ["LIVINGRUNTIME_VAULT_SOCKET"] = "/nonexistent/socket"
        with self.assertRaisesRegex(RuntimeError, "refusing insecure fallback"):
            vault_client.call("list_credentials")

    def test_unprivileged_client_cannot_resolve_raw_secret(self):
        r = vault_client.call("ping")
        self.assertEqual(r["protocol"], 1)
        with self.assertRaises(RuntimeError):
            vault_client.call("resolve_secret", handle="any")
        with self.assertRaises(RuntimeError):
            vault_client.call("github_identity", lease_id="fake", project=None, device=None)

    def test_ciphertext_round_trip_only_metadata_and_scoped_lease(self):
        form = vault_client.call(
            "input_begin", handle="example", provider="github",
            capability="github.identity", project="test", device="host",
        )
        browser = ec.generate_private_key(ec.SECP256R1())
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), base64.b64decode(form["server_public_key"]),
        )
        shared = browser.exchange(ec.ECDH(), peer)
        key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=form["session_id"].encode(), info=b"livingruntime-secure-credential-v1",
        ).derive(shared)
        public = browser.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        ready = vault_client.call(
            "input_exchange", session_id=form["session_id"], client_public_key=b64(public),
        )
        self.assertTrue(ready["ready"])
        secret = "synthetic-test-key-not-a-real-credential"
        iv = secrets.token_bytes(12)
        cipher = AESGCM(key).encrypt(
            iv, secret.encode(), (form["session_id"] + "|store|1").encode(),
        )
        result = vault_client.call(
            "input_submit", session_id=form["session_id"], nonce=b64(iv),
            ciphertext=b64(cipher),
        )
        self.assertEqual(result["handle"], "example")
        self.assertNotIn(secret, str(result))
        self.assertEqual(vault_client.call("list_credentials")["credentials"][0]["handle"], "example")
        lease = vault_client.call(
            "lease_credential", handle="example", capability="github.identity",
            project="test", device="host",
        )
        self.assertNotIn(secret, str(lease))
        self.assertTrue(vault_client.call("revoke_lease", lease_id=lease["lease_id"])["revoked_at"])
        vault_client.call("delete_credential", handle="example")
        self.assertEqual(vault_client.call("list_credentials")["credentials"], [])

    def test_socket_rejects_incorrect_client_uid(self):
        self.server.allowed_uid = os.getuid() + 100
        with self.assertRaises(RuntimeError):
            vault_client.call("ping")

if __name__ == "__main__":
    unittest.main()

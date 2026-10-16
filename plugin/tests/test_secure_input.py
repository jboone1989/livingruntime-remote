from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import credentials
import secure_input


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def exchange_keys(form: dict) -> bytes:
    client = ec.generate_private_key(ec.SECP256R1())
    server = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), base64.b64decode(form["server_public_key"])
    )
    material = client.exchange(ec.ECDH(), server)
    key = HKDF(algorithm=hashes.SHA256(), length=32,
               salt=form["session_id"].encode(), info=secure_input.TAG).derive(material)
    public = client.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert secure_input.exchange(form["session_id"], b64(public))["ready"]
    return key


def encrypted(form: dict, key: bytes, message: bytes) -> tuple[str, str]:
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key).encrypt(
        nonce, message, (form["session_id"] + "|store|1").encode()
    )
    return b64(nonce), b64(cipher)


class SecureCredentialInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LIVINGRUNTIME_REMOTE_CREDENTIALS": str(Path(self.temp.name) / "credentials")
        })
        self.env.start()
        self.form = secure_input.begin(
            handle="test.github", provider="github", capability="github.identity",
            project="repo", device="host-test",
        )
        self.session_id = self.form["session_id"]

    def tearDown(self):
        try:
            secure_input.cancel(self.session_id)
        except PermissionError:
            pass
        self.env.stop()
        self.temp.cleanup()

    def test_encrypted_multiline_key_never_returns_secret(self):
        secret = "-----BEGIN TEST KEY-----\nAbC+123\n-----END TEST KEY-----"
        key = exchange_keys(self.form)
        nonce, cipher = encrypted(self.form, key, secret.encode())
        receipt = secure_input.submit(self.session_id, nonce, cipher)
        self.assertTrue(receipt["stored"])
        self.assertEqual(receipt["handle"], "test.github")
        self.assertNotIn(secret, str(receipt))
        self.assertNotIn(secret, str(self.form))
        entry = credentials.describe("test.github")
        self.assertTrue(entry["secret_permissions_ok"])
        self.assertEqual(entry["projects"], ["repo"])
        self.assertEqual(entry["devices"], ["host-test"])
        self.assertEqual(entry["capabilities"], ["github.identity"])
        with self.assertRaises(PermissionError):
            secure_input.submit(self.session_id, nonce, cipher)
        with self.assertRaises(PermissionError):
            credentials.create_lease("test.github", capability="other")
        with self.assertRaises(PermissionError):
            credentials.create_lease("test.github", capability="github.identity", device="wrong")
        lease = credentials.create_lease(
            "test.github", capability="github.identity", project="repo", device="host-test"
        )
        self.assertNotIn(secret, str(lease))
        self.assertEqual(credentials.resolve_secret_for_lease(
            lease["lease_id"], capability="github.identity", project="repo",
            device="host-test", provider="github",
        ), secret)

    def test_reject_tamper_wrong_public_key_and_failed_replay(self):
        with self.assertRaises(ValueError):
            secure_input.exchange(self.session_id, b64(b"x"))
        key = exchange_keys(self.form)
        bad_client = ec.generate_private_key(ec.SECP256R1()).public_key()
        pub = bad_client.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        with self.assertRaises(PermissionError):
            secure_input.exchange(self.session_id, b64(pub))
        nonce, cipher = encrypted(self.form, key, b"dummy")
        corrupted = bytearray(base64.b64decode(cipher))
        corrupted[-1] ^= 0x01
        with self.assertRaises(Exception):
            secure_input.submit(self.session_id, nonce, b64(corrupted))
        with self.assertRaises(PermissionError):
            secure_input.submit(self.session_id, nonce, cipher)
        self.assertEqual(credentials.list_handles(), [])

    def test_cannot_submit_without_handshake(self):
        nonce = b64(b"0" * 12)
        with self.assertRaises(PermissionError):
            secure_input.submit(self.session_id, nonce, b64(b"x" * 18))
        with self.assertRaises(PermissionError):
            secure_input.exchange(self.session_id, b64(b"x" * 65))

    def test_session_expires_and_wipes_key(self):
        key = exchange_keys(self.form)
        form = secure_input._forms[self.session_id]
        form.expires_at = time.monotonic() - 1
        with self.assertRaises(PermissionError):
            secure_input.submit(self.session_id, *encrypted(self.form, key, b"expired"))
        self.assertIsNone(form.key)
        self.assertEqual(credentials.list_handles(), [])

    def test_bad_handle_and_capability_fail_without_opening_form(self):
        for args in [
            {"handle": "../evil", "provider": "github", "capability": "github.identity"},
            {"handle": "safe", "provider": "github", "capability": ""},
            {"handle": "safe", "provider": "bad provider", "capability": "good"},
        ]:
            with self.assertRaises(ValueError):
                secure_input.begin(project=None, device="test-host", **args)


if __name__ == "__main__":
    unittest.main()

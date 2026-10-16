from __future__ import annotations

import base64
import os
import secrets
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import secure_terminal


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode()


def key_exchange(session: dict[str, object]):
    client = ec.generate_private_key(ec.SECP256R1())
    server = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), base64.b64decode(session["server_public_key"])
    )
    raw = client.exchange(ec.ECDH(), server)
    key = HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=session["session_id"].encode(),
        info=b"livingruntime-secure-terminal-v1",
    ).derive(raw)
    pub = client.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return key, b64(pub)


def encrypted_input(session_id: str, key: bytes, seq: int, blob: bytes) -> tuple[str, str]:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, blob, f"{session_id}|in|{seq}".encode())
    return b64(nonce), b64(ciphertext)


class SecureTerminalTests(unittest.TestCase):
    def setUp(self):
        if os.name == "nt":
            self.skipTest("POSIX PTY tests")
        self.session = secure_terminal.create(
            [sys.executable, "-u", "-c",
             "import sys; print('PROMPT', flush=True); "
             "sys.stdin.readline(); print('ACK', flush=True)"],
            cwd=str(Path.cwd()), ssh_host=None,
        )
        self.session_id = self.session["session_id"]

    def tearDown(self):
        try:
            secure_terminal.close(self.session_id)
        except PermissionError:
            pass

    def test_end_to_end_encrypted_io_and_replay_rejection(self):
        key, pub = key_exchange(self.session)
        self.assertTrue(secure_terminal.exchange(self.session_id, pub)["ready"])
        self.assertTrue(secure_terminal.exchange(self.session_id, pub)["ready"])
        with self.assertRaises(PermissionError):
            secure_terminal.exchange(self.session_id, b64(b"invalid"))
        nonce, cipher = encrypted_input(self.session_id, key, 1, b"fake-test-password\n")
        response = secure_terminal.send(self.session_id, 1, nonce, cipher)
        self.assertTrue(response["accepted"])
        self.assertNotIn("fake-test-password", str(response))
        with self.assertRaises(PermissionError):
            secure_terminal.send(self.session_id, 1, nonce, cipher)
        seen = b""
        for _ in range(100):
            packet = secure_terminal.read(self.session_id)
            self.assertNotIn("PROMPT", str(packet))
            self.assertNotIn("ACK", str(packet))
            if packet["has_output"]:
                raw = AESGCM(key).decrypt(
                    base64.b64decode(packet["nonce"]),
                    base64.b64decode(packet["ciphertext"]),
                    f"{self.session_id}|out|{packet['seq']}".encode(),
                )
                seen += raw
            if packet["running"] is False and b"ACK" in seen:
                break
            time.sleep(0.02)
        self.assertIn(b"PROMPT", seen)
        self.assertIn(b"ACK", seen)
        self.assertNotIn(b"fake-test-password", seen)  # PTY echo must be disabled

    def test_aead_tamper_and_sequence_gap_are_rejected(self):
        key, pub = key_exchange(self.session)
        secure_terminal.exchange(self.session_id, pub)
        nonce, cipher = encrypted_input(self.session_id, key, 1, b"hidden\n")
        with self.assertRaises(PermissionError):
            secure_terminal.send(self.session_id, 2, nonce, cipher)
        bad = bytearray(base64.b64decode(cipher)); bad[-1] ^= 1
        with self.assertRaises(Exception):
            secure_terminal.send(self.session_id, 1, nonce, b64(bad))
        self.assertTrue(secure_terminal.send(self.session_id, 1, nonce, cipher)["accepted"])

    def test_expiry_closes_session_and_denies_access(self):
        key, pub = key_exchange(self.session)
        secure_terminal.exchange(self.session_id, pub)
        with secure_terminal._lock:
            secure_terminal._sessions[self.session_id].expires_at = time.monotonic() - 1
        with self.assertRaises(PermissionError):
            secure_terminal.read(self.session_id)

    def test_no_plaintext_before_handshake(self):
        packet = secure_terminal.create(
            [sys.executable, "-u", "-c", "print('never started')"], cwd=str(Path.cwd()),
        )
        try:
            with self.assertRaises(PermissionError):
                secure_terminal.read(packet["session_id"])
            with self.assertRaises(PermissionError):
                secure_terminal.send(packet["session_id"], 1, b64(b"0"*12), b64(b"0"*17))
        finally:
            secure_terminal.close(packet["session_id"])

    def test_oversized_and_wrong_key_fail_closed(self):
        key, pub = key_exchange(self.session)
        with self.assertRaises(ValueError):
            secure_terminal.exchange(self.session_id, b64(b"short"))
        secure_terminal.exchange(self.session_id, pub)
        nonce, cipher = encrypted_input(self.session_id, b"0"*32, 1, b"input\n")
        with self.assertRaises(Exception):
            secure_terminal.send(self.session_id, 1, nonce, cipher)


if __name__ == "__main__":
    unittest.main()

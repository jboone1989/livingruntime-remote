"""Ephemeral encrypted credential intake for the Connector-local credential broker.

The client encrypts locally and no tool accepts a plaintext secret. Only a
bounded, one-time credential-store operation is supported; no generic eval,
shell, or process input is exposed by this module.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from credentials import MAX_SECRET_BYTES, HANDLE_RE, set_local_secret

MAX_FORMS = 8
TTL_SECONDS = 180
TAG = b"livingruntime-secure-credential-v1"
NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_lock = threading.RLock()
_forms: dict[str, "InputForm"] = {}
_sweeper_started = False


def _decode(value: str, max_size: int) -> bytes:
    if not isinstance(value, str) or len(value) > (max_size + 3) * 2:
        raise ValueError("invalid encrypted input")
    data = base64.b64decode(value, validate=True)
    if len(data) > max_size:
        raise ValueError("encrypted input exceeds limit")
    return data


def _public_bytes(key: ec.EllipticCurvePrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )).decode("ascii")


@dataclass
class InputForm:
    session_id: str
    handle: str
    provider: str
    capability: str
    project: str | None
    device: str
    expires_at: float
    private: ec.EllipticCurvePrivateKey | None = field(repr=False)
    key: bytes | None = field(default=None, repr=False)
    client_public_key: str | None = field(default=None, repr=False)

    def key_exchange(self, client_public_key: str) -> dict[str, Any]:
        if self.key is not None:
            if client_public_key != self.client_public_key:
                raise PermissionError("secure input already bound to a different browser key")
            return {"session_id": self.session_id, "ready": True}
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), _decode(client_public_key, 65)
        )
        shared = self.private.exchange(ec.ECDH(), peer)
        self.key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=self.session_id.encode("ascii"), info=TAG,
        ).derive(shared)
        self.client_public_key = client_public_key
        self.private = None
        return {"session_id": self.session_id, "ready": True}

    def submit(self, nonce: str, ciphertext: str) -> dict[str, Any]:
        if self.key is None:
            raise PermissionError("secure input handshake required")
        iv = _decode(nonce, 12)
        if len(iv) != 12:
            raise ValueError("invalid encrypted input")
        data = AESGCM(self.key).decrypt(
            iv, _decode(ciphertext, MAX_SECRET_BYTES + 16),
            (self.session_id + "|store|1").encode("ascii"),
        )
        if not data or len(data) > MAX_SECRET_BYTES:
            raise ValueError("credential input must be 1 to 65536 bytes")
        try:
            secret = data.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ValueError("credential input must be UTF-8") from exc
        # Existing trusted local credential API; result is metadata only.
        stored = set_local_secret(
            self.handle, secret,
            provider=self.provider,
            capabilities=[self.capability],
            projects=[self.project] if self.project else [],
            devices=[self.device],
        )
        return {
            "stored": True, "handle": stored["handle"],
            "provider": stored["provider"],
            "capabilities": stored["capabilities"],
            "projects": stored["projects"], "devices": stored["devices"],
            "secret_present": stored["secret_present"],
        }

    def wipe(self) -> None:
        self.private = None
        self.key = None
        self.client_public_key = None


def _cleanup() -> None:
    now = time.monotonic()
    for ident, form in list(_forms.items()):
        if form.expires_at <= now:
            _forms.pop(ident).wipe()


def _sweep() -> None:
    while True:
        time.sleep(10)
        with _lock:
            _cleanup()


def begin(
    *, handle: str, provider: str, capability: str,
    project: str | None, device: str,
) -> dict[str, Any]:
    """Create a browser-only public key form; secret input is never an argument."""
    global _sweeper_started
    if os.name == "nt":
        raise RuntimeError(
            "protected credential entry requires a POSIX Connector; "
            "Windows credential storage needs a native ACL/keychain backend"
        )
    if not isinstance(handle, str) or HANDLE_RE.fullmatch(handle) is None:
        raise ValueError("invalid credential handle")
    if not isinstance(provider, str) or NAME_RE.fullmatch(provider) is None:
        raise ValueError("invalid credential provider")
    if not isinstance(capability, str) or NAME_RE.fullmatch(capability) is None:
        raise ValueError("invalid credential capability")
    if not isinstance(device, str) or not device or len(device) > 128:
        raise ValueError("target host is required")
    if project is not None and (not isinstance(project, str) or len(project) > 128):
        raise ValueError("invalid project")
    with _lock:
        _cleanup()
        if len(_forms) >= MAX_FORMS:
            raise RuntimeError("too many pending secure inputs")
        private = ec.generate_private_key(ec.SECP256R1())
        session_id = "secret_" + secrets.token_urlsafe(24)
        form = InputForm(
            session_id, handle, provider, capability, project, device,
            time.monotonic() + TTL_SECONDS, private,
        )
        _forms[session_id] = form
        if not _sweeper_started:
            threading.Thread(target=_sweep, daemon=True, name="secure-input-expiry").start()
            _sweeper_started = True
        return {
            "session_id": session_id,
            "server_public_key": _public_bytes(private),
            "expires_in": TTL_SECONDS,
            "handle": handle, "provider": provider, "capability": capability,
            "project": project, "device": device,
        }


def _get(session_id: str) -> InputForm:
    _cleanup()
    if not isinstance(session_id, str) or session_id not in _forms:
        raise PermissionError("unknown or expired secure input")
    return _forms[session_id]


def exchange(session_id: str, client_public_key: str) -> dict[str, Any]:
    with _lock:
        return _get(session_id).key_exchange(client_public_key)


def submit(session_id: str, nonce: str, ciphertext: str) -> dict[str, Any]:
    with _lock:
        form = _get(session_id)
        # Consume the form before decrypting, so a failed submission cannot
        # be replayed or used as an unlimited decryption oracle.
        _forms.pop(session_id)
        try:
            return form.submit(nonce, ciphertext)
        finally:
            form.wipe()


def cancel(session_id: str) -> dict[str, Any]:
    with _lock:
        form = _get(session_id)
        _forms.pop(session_id)
        form.wipe()
        return {"cancelled": True, "session_id": session_id}

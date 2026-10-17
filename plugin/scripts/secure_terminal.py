"""Ephemeral, end-to-end encrypted interactive PTY sessions.

Only the user's browser has the client ECDH private key. Relay jobs, MCP
parameters, model transcripts and audit records carry ciphertext, never stdin
or terminal output. No session secrets or terminal transcripts are persisted.
"""
from __future__ import annotations

import base64
import os
import secrets
import select
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TTL_SECONDS = 300
MAX_SESSIONS = 4
MAX_INPUT = 4096
MAX_OUTPUT = 8192
_sessions: dict[str, "TerminalSession"] = {}
_lock = threading.RLock()
_sweeper_started = False


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * limit:
        raise ValueError("invalid encrypted field length")
    data = base64.b64decode(value, validate=True)
    if len(data) > limit:
        raise ValueError("encrypted field exceeds limit")
    return data


@dataclass
class TerminalSession:
    session_id: str
    argv: list[str]
    cwd: str
    ssh_host: str | None
    expires_at: float
    private_key: Any
    public_key: str
    key: bytes | None = None
    client_key: str | None = None
    process: Any = None
    fd: int | None = None
    seq_in: int = 0
    seq_out: int = 0

    def open(self, client_key: str) -> dict[str, Any]:
        if self.key is not None:
            if self.client_key != client_key:
                raise PermissionError("terminal key exchange is already bound")
            return {"session_id": self.session_id, "ready": True}
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), _unb64(client_key, 65)
        )
        shared = self.private_key.exchange(ec.ECDH(), peer)
        key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=self.session_id.encode(), info=b"livingruntime-secure-terminal-v1",
        ).derive(shared)
        # Do not start the process before successful key validation.
        if self.ssh_host is not None:
            # Explicit argv, no user-supplied shell text. Strict SSH host-key
            # checking and key-only authentication; no connector credential upload.
            command = (
                "cd " + shlex.quote(self.cwd)
                + " && stty -echo && exec " + shlex.join(self.argv)
            )
            self.process = subprocess.Popen(
                ["ssh", "-tt", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=yes",
                 "-o", "ConnectTimeout=10", self.ssh_host, command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True,
            )
            self.fd = self.process.stdout.fileno()
        else:
            import pty
            import termios
            master, slave = pty.openpty()
            try:
                attrs = termios.tcgetattr(slave)
                attrs[3] &= ~termios.ECHO
                termios.tcsetattr(slave, termios.TCSANOW, attrs)
                self.process = subprocess.Popen(
                    self.argv, cwd=self.cwd, stdin=slave, stdout=slave,
                    stderr=slave, start_new_session=True, close_fds=True,
                )
                self.fd = master
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
        self.client_key = client_key
        self.key = key
        self.private_key = None
        return {"session_id": self.session_id, "ready": True}

    def send(self, seq: int, nonce: str, ciphertext: str) -> dict[str, Any]:
        if self.key is None or self.process is None:
            raise PermissionError("terminal handshake required")
        if type(seq) is not int or seq != self.seq_in + 1:
            raise PermissionError("out-of-order or replayed terminal input")
        raw_nonce = _unb64(nonce, 12)
        if len(raw_nonce) != 12:
            raise ValueError("invalid nonce")
        raw = AESGCM(self.key).decrypt(
            raw_nonce, _unb64(ciphertext, MAX_INPUT + 16),
            f"{self.session_id}|in|{seq}".encode(),
        )
        if not raw or len(raw) > MAX_INPUT:
            raise ValueError("terminal input outside allowed bounds")
        if self.process.poll() is not None:
            raise RuntimeError("terminal process has exited")
        target = self.process.stdin.fileno() if self.ssh_host else self.fd
        os.write(target, raw)
        self.seq_in = seq
        return {"accepted": True, "seq": seq}

    def read(self) -> dict[str, Any]:
        if self.key is None or self.fd is None:
            raise PermissionError("terminal handshake required")
        data = b""
        if select.select([self.fd], [], [], 0)[0]:
            try:
                data = os.read(self.fd, MAX_OUTPUT)
            except OSError as exc:
                # PTY master reports EIO after child exits.
                if exc.errno != 5:
                    raise
        status = self.process.poll()
        result: dict[str, Any] = {
            "session_id": self.session_id, "running": status is None,
            "returncode": status, "has_output": bool(data),
        }
        if data:
            self.seq_out += 1
            nonce = secrets.token_bytes(12)
            result.update({
                "seq": self.seq_out, "nonce": _b64(nonce),
                "ciphertext": _b64(AESGCM(self.key).encrypt(
                    nonce, data, f"{self.session_id}|out|{self.seq_out}".encode()
                )),
            })
        return result

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
        if self.fd is not None and self.ssh_host is None:
            os.close(self.fd)
        self.key = None
        self.private_key = None


def _cleanup() -> None:
    now = time.monotonic()
    for ident, session in list(_sessions.items()):
        if session.expires_at <= now:
            _sessions.pop(ident)
            session.close()


def _sweep() -> None:
    while True:
        time.sleep(10)
        with _lock:
            _cleanup()


def create(argv: list[str], cwd: str, ssh_host: str | None = None) -> dict[str, Any]:
    global _sweeper_started
    if os.name == "nt":
        raise RuntimeError("secure terminal PTY currently requires a POSIX connector")
    with _lock:
        _cleanup()
        if len(_sessions) >= MAX_SESSIONS:
            raise RuntimeError("too many secure terminal sessions")
        ident = "term_" + secrets.token_urlsafe(24)
        private = ec.generate_private_key(ec.SECP256R1())
        pub = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        session = TerminalSession(
            ident, list(argv), cwd, ssh_host, time.monotonic() + TTL_SECONDS,
            private, _b64(pub),
        )
        _sessions[ident] = session
        if not _sweeper_started:
            threading.Thread(target=_sweep, daemon=True, name="terminal-expiry").start()
            _sweeper_started = True
        return {
            "session_id": ident, "server_public_key": session.public_key,
            "ttl_seconds": TTL_SECONDS, "transport": "P-256 ECDH + HKDF + AES-256-GCM",
        }


def _get(session_id: str) -> TerminalSession:
    _cleanup()
    if not isinstance(session_id, str) or session_id not in _sessions:
        raise PermissionError("unknown or expired terminal session")
    return _sessions[session_id]


def exchange(session_id: str, client_public_key: str) -> dict[str, Any]:
    with _lock:
        return _get(session_id).open(client_public_key)


def send(session_id: str, seq: int, nonce: str, ciphertext: str) -> dict[str, Any]:
    with _lock:
        return _get(session_id).send(seq, nonce, ciphertext)


def read(session_id: str) -> dict[str, Any]:
    with _lock:
        return _get(session_id).read()


def close(session_id: str) -> dict[str, Any]:
    with _lock:
        session = _get(session_id)
        del _sessions[session_id]
        session.close()
        return {"closed": True, "session_id": session_id}

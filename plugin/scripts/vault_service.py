"""Independent owner-only credential vault over a Unix domain socket.

Run this process under a dedicated OS uid, never the development executor uid.
No RPC ever returns raw credential bytes.  The caller supplies ciphertext only;
ECDH private keys and all decrypted values exist inside this broker process.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import stat
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import credentials
import secure_input

MAX_REQUEST = 160 * 1024
MAX_RESPONSE = 160 * 1024
ALLOWED = frozenset({
    "ping", "input_begin", "input_exchange", "input_submit", "input_cancel",
    "list_credentials", "delete_credential", "lease_credential",
    "list_leases", "revoke_lease", "github_identity",
})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise PermissionError("provider verification redirects forbidden")


def github_identity(lease_id: str, project: str | None, device: str | None) -> dict[str, Any]:
    """The only outbound secret consumer. Never return raw token or response headers."""
    secret = credentials.resolve_secret_for_lease(
        lease_id, capability="github.identity", project=project,
        device=device, provider="github",
    )
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "authorization": "Bearer " + secret,
            "accept": "application/vnd.github+json",
            "user-agent": "LivingRuntime-Remote-Vault/1",
            "x-github-api-version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("GitHub verification returned a non-200 status")
            body = response.read(65537)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub verification failed HTTP {exc.code}") from None
    except (OSError, TimeoutError, urllib.error.URLError):
        raise RuntimeError("GitHub verification failed") from None
    if len(body) > 65536:
        raise RuntimeError("GitHub verification response exceeded limit")
    try:
        profile = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("GitHub verification returned invalid JSON") from None
    if not isinstance(profile, dict):
        raise RuntimeError("GitHub verification returned invalid identity")
    return {
        "provider": "github", "authenticated": True,
        "login": profile.get("login"), "id": profile.get("id"),
        "name": profile.get("name"), "type": profile.get("type"),
    }


def dispatch(message: dict[str, Any]) -> dict[str, Any]:
    """Strict allowlist, not a generic function lookup nor a general-purpose proxy."""
    operation = message.get("op")
    if operation not in ALLOWED:
        raise PermissionError("vault operation is not allowed")
    a = message.get("args")
    if not isinstance(a, dict):
        raise ValueError("vault request arguments must be an object")
    if operation == "ping":
        if a:
            raise ValueError("ping does not accept parameters")
        return {"ok": True, "isolated": True, "protocol": 1}
    if operation == "input_begin":
        return secure_input.begin(**a)
    if operation == "input_exchange":
        return secure_input.exchange(**a)
    if operation == "input_submit":
        return secure_input.submit(**a)
    if operation == "input_cancel":
        return secure_input.cancel(**a)
    if operation == "list_credentials":
        if a:
            raise ValueError("unexpected parameters")
        return {"credentials": credentials.list_handles()}
    if operation == "delete_credential":
        handle = a["handle"]
        if set(a) != {"handle"}:
            raise ValueError("unexpected parameters")
        # Reject unknown names instead of silently reporting a no-op.
        existing = credentials.describe(handle)
        credentials.remove_local_secret(handle)
        return {"deleted": existing["handle"]}
    if operation == "lease_credential":
        return credentials.create_lease(**a)
    if operation == "list_leases":
        return {"leases": credentials.list_leases(**a)}
    if operation == "revoke_lease":
        return credentials.revoke_lease(**a)
    if operation == "github_identity":
        return github_identity(**a)
    raise PermissionError("vault operation is not allowed")


class VaultHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(15)
        # SO_PEERCRED checks the OS uid even if some other process gets socket access.
        pid, uid, gid = struct.unpack(
            "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != self.server.allowed_uid:
            self.request.sendall(b'{"ok":false,"error":"unauthorized client"}\n')
            return
        buffer = bytearray()
        while len(buffer) <= MAX_REQUEST and b"\n" not in buffer:
            data = self.request.recv(min(65536, MAX_REQUEST + 2 - len(buffer)))
            if not data:
                return
            buffer.extend(data)
        if b"\n" not in buffer or len(buffer) > MAX_REQUEST + 1:
            self.request.sendall(b'{"ok":false,"error":"request too large"}\n')
            return
        try:
            req = json.loads(bytes(buffer).split(b"\n", 1)[0].decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("invalid request object")
            result = dispatch(req)
            response = {"ok": True, "result": result}
        except Exception as exc:
            # Expose no attacker-controlled repr, ciphertext, secret, or traceback.
            kind = "permission denied" if isinstance(exc, PermissionError) else "invalid or failed vault operation"
            response = {"ok": False, "error": kind}
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_RESPONSE:
            encoded = b'{"ok":false,"error":"response too large"}\n'
        self.request.sendall(encoded)


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class VaultServer(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, address: str, allowed_uid: int):
        self.allowed_uid = allowed_uid
        super().__init__(address, VaultHandler)


def run(socket_path: Path, root: Path, allowed_uid: int) -> None:
    if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("the isolated credential vault requires Linux SO_PEERCRED")
    if os.geteuid() == 0:
        raise PermissionError("vault daemon must not run as root")
    if os.geteuid() == allowed_uid:
        raise PermissionError("vault must run under a different OS uid from Connector")
    os.umask(0o077)
    if socket_path.exists() or socket_path.is_symlink():
        previous = socket_path.lstat()
        if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.geteuid():
            raise FileExistsError("unexpected file at vault socket path")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1)
            probe.connect(str(socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            # An owned socket left behind after an unclean service restart.
            socket_path.unlink()
        else:
            raise FileExistsError("vault service is already listening")
        finally:
            probe.close()
    if not socket_path.parent.is_dir():
        raise ValueError("systemd must create the vault socket directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    st = root.stat()
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise PermissionError("vault store must be owned by vault service with mode 0700")
    os.environ["LIVINGRUNTIME_REMOTE_CREDENTIALS"] = str(root)
    with VaultServer(str(socket_path), allowed_uid) as server:
        socket_path.chmod(0o660)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/run/livingruntime-vault/vault.sock")
    parser.add_argument("--store", default="/var/lib/livingruntime-vault/credentials")
    parser.add_argument("--connector-uid", type=int, required=True)
    args = parser.parse_args()
    run(Path(args.socket), Path(args.store), args.connector_uid)


if __name__ == "__main__":
    main()

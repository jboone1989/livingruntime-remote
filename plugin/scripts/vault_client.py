"""Secret-free client for the separately privileged LivingRuntime vault service."""
from __future__ import annotations

import json
import os
import socket
from typing import Any

DEFAULT_SOCKET = "/run/livingruntime-vault/vault.sock"
MAX_RESPONSE = 160 * 1024


def call(operation: str, **kwargs: Any) -> dict[str, Any]:
    """No automatic fallback to the old user-readable credential store."""
    payload = json.dumps({"op": operation, "args": kwargs}, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_RESPONSE:
        raise ValueError("vault request too large")
    address = os.environ.get("LIVINGRUNTIME_VAULT_SOCKET", DEFAULT_SOCKET)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15)
            client.connect(address)
            client.sendall(payload)
            pieces = bytearray()
            while b"\n" not in pieces and len(pieces) <= MAX_RESPONSE:
                piece = client.recv(min(65536, MAX_RESPONSE + 2 - len(pieces)))
                if not piece:
                    break
                pieces.extend(piece)
    except (OSError, TimeoutError) as exc:
        raise RuntimeError("isolated credential vault unavailable; refusing insecure fallback") from None
    if b"\n" not in pieces or len(pieces) > MAX_RESPONSE:
        raise RuntimeError("invalid vault response")
    try:
        result = json.loads(bytes(pieces).split(b"\n", 1)[0])
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("invalid vault response") from None
    if not isinstance(result, dict):
        raise RuntimeError("invalid vault response")
    if result.get("ok") is not True:
        raise RuntimeError(str(result.get("error") or "vault operation denied")[:120])
    value = result.get("result")
    if not isinstance(value, dict):
        raise RuntimeError("invalid vault receipt")
    return value

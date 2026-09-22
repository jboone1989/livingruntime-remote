from __future__ import annotations

import hashlib
import json

REMOTE_IDENTITY = "livingruntime.remote"

REMOTE_TOOLS = (
    "connection_status",
    "capabilities",
    "list_projects",
    "read_file",
    "write_file",
    "list_dir",
    "git",
    "exec",
    "process",
    "systemd",
    "logs",
    "apply_patch",
    "diagnostics",
)

# Handshake core surface advertised in capabilities.tools. MCP still registers
# the full REMOTE_TOOLS list; do not treat this as a second registry.
HANDSHAKE_TOOLS = (
    "connection_status",
    "read_file",
    "write_file",
    "git",
    "exec",
    "logs",
    "apply_patch",
)

# Backward-compatible aliases. Do not maintain a smaller subset.
CORE_TOOLS = REMOTE_TOOLS
DISCOVERY_TOOLS = REMOTE_TOOLS

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_HTTP_PORT = 8766
PLUGIN_NAME = "LivingRuntime Remote"
PLUGIN_VERSION = "0.4.7"
SECRET_KEYS = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "identity_file",
)


def schema_hash() -> str:
    payload = {
        "identity": REMOTE_IDENTITY,
        "tools": list(REMOTE_TOOLS),
        "version": PLUGIN_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

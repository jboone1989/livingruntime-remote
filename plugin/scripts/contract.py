from __future__ import annotations

import hashlib
import json

REMOTE_IDENTITY = "livingruntime.remote"

REMOTE_TOOLS = (
    "connection_status",
    "capabilities",
    "list_devices",
    "remote_overview",
    "list_projects",
    "read_file",
    "write_file",
    "list_dir",
    "git",
    "exec",
    "list_credentials",
    "lease_credential",
    "list_credential_leases",
    "revoke_credential_lease",
    "github_identity",
    "create_job",
    "get_job",
    "list_jobs",
    "checkpoint_job",
    "list_exec_permissions",
    "approve_exec_permission",
    "deny_exec_permission",
    "revoke_exec_permission",
    "process",
    "systemd",
    "logs",
    "apply_patch",
    "diagnostics",
    "start_pi_step",
    "watch_pi_job",
    "wait_pi_job_completion",
    "bind_openai_pi_continuation",
    "continue_openai_pi_job",
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
PLUGIN_VERSION = "0.4.23"
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

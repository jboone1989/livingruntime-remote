# LivingRuntime Remote Public Relay

This service is the public-plugin transport for LivingRuntime Remote. It is not an SSH gateway and does not hold user SSH credentials.

Flow:

```text
ChatGPT
  -> OAuth 2.1 / PKCE
  -> https://remote.livingruntime.com/mcp
  -> per-user relay task
  <- outbound HTTPS long poll
LivingRuntime Remote connector
  -> existing bounded SSH bridge
  -> user's configured host/repositories
```

The cloud relay stores OAuth subject/device bindings, hashed device tokens, short-lived one-time pairing codes, and bounded in-flight task/result payloads. SSH keys, SSH passwords, and repository credentials stay on the user's connector machine.

## OAuth requirements

Production uses the embedded OAuth/OIDC mode:

```text
LIVINGRUNTIME_AUTH_MODE=embedded
LIVINGRUNTIME_RELAY_ISSUER=https://remote.livingruntime.com
LIVINGRUNTIME_RELAY_AUDIENCE=https://remote.livingruntime.com/mcp
LIVINGRUNTIME_RELAY_RESOURCE_URL=https://remote.livingruntime.com/mcp
LIVINGRUNTIME_RELAY_DOCS_URL=https://remote.livingruntime.com/support
OPENAI_APPS_CHALLENGE=<domain-verification-token>
```

The embedded provider supports dynamic public-client registration, authorization code + PKCE, refresh-token rotation, OIDC discovery, UserInfo, and the standard `offline_access` scope used by ChatGPT to maintain long-lived connections. Tokens must include `remote:read`; modifying tools additionally require `remote:write`. `openid` and `email` provide the authenticated identity used by the relay.

Protected-resource metadata is served at:

```text
/.well-known/oauth-protected-resource/mcp
```

The service also supports an external OAuth/OIDC verifier mode when an operator deliberately configures one. SSH credentials never move into either OAuth mode.

The Python service stays on loopback and enforces a basic per-client `/device/pair` attempt limit. The HTTPS edge terminates TLS and should provide an additional abuse/rate-limit layer.

## Run

The relay requires Python 3.10 or newer. Keep its interpreter and venv outside privileged home directories when the service runs as a dedicated system user.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
LIVINGRUNTIME_RELAY_DB=/var/lib/livingruntime-remote/relay.sqlite3 \
  .venv/bin/python server.py
```

Reusable production templates live under `deploy/`: the hardened systemd unit, an environment example, and a Caddy reverse-proxy snippet.

Health check:

```text
GET /healthz
```

MCP endpoint:

```text
POST /mcp
```

## Pair a user's connector

After signing into the plugin, the user asks ChatGPT to call `create_pairing_code`. On the machine that can already reach their development host:

```bash
python plugin/scripts/relay_agent.py \
  --url https://remote.livingruntime.com \
  --pair ABCD-EFGH
```

The returned device token is written only to `~/.livingruntime/relay.json` with user-only permissions where supported. The server stores only its SHA-256 digest.

Then keep the connector running:

```bash
python plugin/scripts/relay_agent.py
```

The connector accepts only the explicit LivingRuntime Remote tool allowlist and delegates each call to the existing bounded bridge.

## Production notes

SQLite is sufficient for one relay process and the initial reviewed beta. Idle connector long polls and in-flight MCP result waits are event-driven inside the relay process: they do not repeatedly query SQLite while waiting for work or completion. Before horizontal scaling, replace the in-process wakeups plus SQLite task queue with a shared broker/database while preserving the same user/device ownership checks. Do not put sticky SSH credentials in the relay to avoid building a second secret-management plane.

The public edge must provide HTTPS, request/body limits, a second pairing-rate-limit layer, normal abuse controls, and durable service monitoring. A reviewer account should be paired to a dedicated low-privilege review host rather than a production machine.

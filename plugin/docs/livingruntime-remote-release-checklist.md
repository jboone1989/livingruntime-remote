# LivingRuntime Remote release checklist

Identity is `livingruntime.remote`. Current public release version: `0.4.21`.

## Capability and runtime

- [x] `REMOTE_TOOLS` is the canonical capability registry.
- [x] `capabilities` reports identity, version, schema hash, available tools, and explicit missing tools.
- [x] `connection_status` reports runtime/gateway reachability.
- [x] MCP `tools/list` matches `REMOTE_TOOLS`.
- [x] public relay exposes the full Remote surface including dynamic exec approval, `apply_patch`, and `diagnostics`.

## Public auth and pairing

- [x] OAuth + PKCE metadata and token flow.
- [x] OIDC discovery, `openid` / `email` scopes, UserInfo with verified email, and `offline_access` for refreshable ChatGPT sessions.
- [x] protected MCP resource metadata.
- [x] one-time device pairing and revocation.
- [x] device tokens are stored hashed server-side.
- [x] pairing attempts are rate limited.
- [x] offline devices are detected before enqueue.
- [x] unclaimed timed-out work is cancelled.

## Connector 0.4.21

- [x] standalone Connector CLI.
- [x] no Python requirement for release binaries.
- [x] interactive no-argument setup asks only for pairing code, SSH host, and allowed root.
- [x] Windows per-user Scheduled Task autostart.
- [x] Linux systemd user-service autostart.
- [x] macOS LaunchAgent autostart.
- [x] status output never exposes the device token.
- [x] installer bootstrap scripts for PowerShell and POSIX shell.
- [x] public relay install page and download redirects.
- [x] GitHub Actions release matrix for Windows x64, Linux x64/ARM64, macOS Intel/Apple Silicon.
- [x] packaged binary smoke test in release workflow.

## Validation

- [x] plugin self-check.
- [x] plugin unit/integration suite.
- [x] relay OAuth/isolation suite.
- [x] POSIX installer syntax check.
- [x] local PyInstaller Linux single-file build.
- [x] packaged Connector `--version` and `status` smoke test.
- [ ] GitHub v0.4.21 release assets are available for all five target platforms.
- [x] production `remote.livingruntime.com` DNS/TLS is active.
- [ ] Windows Authenticode signing credential configured.
- [ ] Apple Developer ID/notarization credentials configured.
- [x] isolated App Directory reviewer account + demo Connector validated.
- [x] submission import has exactly 5 positive + 3 negative cases and covers all public tool annotations.
- [ ] final ChatGPT marketplace/domain verification and submission completed.

## User flow

1. Install LivingRuntime Remote in ChatGPT and sign in.
2. Create a one-time pairing code.
3. Open `https://remote.livingruntime.com/install`.
4. Run the Connector.
5. Enter pairing code, SSH host, and allowed workspace root.
6. Return to ChatGPT and call `connection_status`.

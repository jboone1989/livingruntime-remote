# Public release path

LivingRuntime Remote is released from this repository alone.

## Public architecture

```text
ChatGPT
  -> LivingRuntime Remote
  -> OAuth-protected HTTPS relay
  -> paired Connector
  -> bounded SSH-backed tools
  -> user's host/project
```

## Distribution

Connector binaries are built by `.github/workflows/release.yml` for Windows x64, Linux x64/ARM64, and macOS Intel/Apple Silicon. Tags use `vX.Y.Z`. The repository is public and release assets are downloadable without repository credentials.

## 0.4.6 release readiness

Implemented:

- production domain `https://remote.livingruntime.com`
- Let's Encrypt TLS on the branded hostname
- OAuth issuer and MCP resource bound to the branded domain
- OAuth 2.1 authorization-code + PKCE and dynamic client registration
- `openid` / `email` scopes, OIDC discovery, and UserInfo for workspace-domain restrictions
- branded home/install/support/privacy/terms routes
- five-platform Connector releases
- standalone public repository and release workflow

## Remaining launch work

- complete the OpenAI portal domain-verification challenge when the portal provides its token
- provide reviewer-ready demo credentials in the submission portal
- scan tools in the submission portal and resolve any portal-reported metadata issues
- submit for ChatGPT/Codex plugin review
- Windows Authenticode signing credentials
- Apple Developer ID/notarization credentials

## Product boundary

This repository does not contain pi-remote. pi-remote may consume LivingRuntime Remote, but LivingRuntime Remote does not depend on pi-remote.

# Public release path

LivingRuntime Remote is released from this repository alone.

## Public architecture

```
ChatGPT
  -> LivingRuntime Remote
  -> OAuth-protected HTTPS relay
  -> paired Connector
  -> bounded SSH-backed tools
  -> user's host/project
```

## Distribution

Connector binaries are built by `.github/workflows/release.yml` for:

- Windows x64
- Linux x64
- Linux ARM64
- macOS Intel
- macOS Apple Silicon

Tags use the normal repository form `vX.Y.Z`.

When this repository becomes public, `remote.livingruntime.com/install` can point directly to the repository's public GitHub Releases.

## 0.4.5 release readiness

Implemented:

- branded home/install/support/privacy/terms routes
- plugin metadata points to `remote.livingruntime.com`
- standalone repository and release workflow
- production relay can run 0.4.5 behind the existing hostname before the DNS cutover

## Remaining launch work

- add DNS `remote.livingruntime.com -> 207.246.99.253` and issue TLS
- switch the production OAuth issuer/resource/docs URLs to `remote.livingruntime.com`
- ChatGPT domain/developer verification and marketplace review
- Windows Authenticode signing credentials
- Apple Developer ID/notarization credentials

## Product boundary

This repository does not contain pi-remote. pi-remote may consume LivingRuntime Remote, but LivingRuntime Remote does not depend on pi-remote.

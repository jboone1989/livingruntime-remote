# LivingRuntime Remote

LivingRuntime Remote is the future open-source remote capability layer of the LivingRuntime ecosystem.

It lets ChatGPT-compatible clients work with machines and repositories the user already controls through bounded, auditable remote tools.

## Product boundary

LivingRuntime Remote is the lightweight foundation:

- authenticated MCP access
- bounded SSH-backed file, Git, process, service, and diagnostic tools
- OAuth-protected public relay
- device pairing
- cross-platform Connector
- one-click installation and release artifacts

pi-remote is a separate private product built above this foundation. Its persistent sessions, planning, checkpoints, autonomous coding workflows, and distributed workers are intentionally not part of this repository.

## Repository layout

- plugin/ - ChatGPT/Codex plugin and MCP runtime
- Connector implementation - plugin/scripts/connector.py
- relay/ - public OAuth relay and device pairing service
- gateway/ - bounded MCP gateway
- .github/workflows/ci.yml - test matrix
- .github/workflows/release.yml - cross-platform Connector release builds

## Current release

Version: 0.4.7

The repository is private during development. Before public launch it will be made public and given an explicit open-source license.

## Install target

Public users will eventually install from remote.livingruntime.com/install. The install page will download Connector binaries from this repository's public GitHub Releases after the repository is opened.

## Security model

- no SSH passwords or private keys are stored by LivingRuntime Remote
- file access is restricted to configured roots
- service operations are allowlisted
- destructive operations are auditable
- public access uses OAuth and one-time device pairing
- connector device tokens are separate from SSH credentials

## Development

See plugin/README.md, plugin/PUBLIC_RELEASE.md, and plugin/docs/.

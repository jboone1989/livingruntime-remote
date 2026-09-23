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

Version: 0.4.23

The repository is private during development. Before public launch it will be made public and given an explicit open-source license.

### Dynamic exec approval V1

Commands outside the built-in development allowlist no longer have to be added in code first. An
`exec` call creates a durable approval request containing the exact argv, target host, project,
working directory, and risk class. Operator approval persists a grant; revocation removes it.

Permissions are host-scoped by default. Only read-only diagnostic commands can receive
`all_owned_hosts` or `diagnostic_class` grants. Shells, privilege escalation, destructive filesystem
commands, and direct systemd/journal control remain hard-denied through dynamic exec authorization.

### Multi-device V1

One Connector can manage multiple configured SSH hosts. `list_devices` discovers the configured hosts and probes their reachability. Remote tools accept an optional `device` ID such as `main` or `vultr`; omitting it preserves the configured default host. Project aliases still route to their bound host, and a conflicting `project` plus `device` selection is rejected instead of silently crossing host boundaries.

Examples:

```text
list_devices()
connection_status(device="vultr")
logs(device="vultr", unit="livingruntime-remote-relay.service")
exec(device="main", argv=["ps"])
```

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

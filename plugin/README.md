# LivingRuntime Remote Plugin

Identity: `livingruntime.remote`

Current version: `0.4.19`

This directory contains the ChatGPT/Codex-facing MCP runtime and the local Connector implementation.

## Capability surface

- connection_status
- capabilities
- list_devices
- list_projects
- read_file
- write_file
- list_dir
- git
- exec
- list_exec_permissions
- approve_exec_permission
- deny_exec_permission
- revoke_exec_permission
- process
- systemd
- logs
- apply_patch
- diagnostics

The canonical registry is `scripts/contract.py`.

### Dynamic exec approvals

`exec` keeps the built-in bounded development command set, but an otherwise unlisted executable
now produces a durable approval request instead of requiring a code change. Approval is exact-command
and host-scoped by default. Read-only diagnostics may be approved for all owned hosts or as a
diagnostic class. Hard-denied shells, privilege escalation, destructive filesystem commands, and
direct system service control cannot be enabled through this path.

Use `list_exec_permissions` to inspect requests/grants, `approve_exec_permission` or
`deny_exec_permission` to decide a request, and `revoke_exec_permission` to remove a grant.

## Public install flow

Public users install the ChatGPT app, create a one-time pairing code, then install the Connector on a computer that already has key-based SSH access to the target Linux host.

Windows:

```powershell
irm https://remote.livingruntime.com/install.ps1 | iex
```

Linux/macOS:

```bash
curl -fsSL https://remote.livingruntime.com/install.sh | sh
```

The Connector asks for only the pairing code, SSH host, and allowed workspace root. SSH credentials remain in the user's normal SSH configuration.

## Local development

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the plugin tests from the repository root:

```bash
python -m unittest discover -s plugin/tests -v
```

Run the manifest self-check:

```bash
python plugin/scripts/selfcheck.py
```

## Security boundary

- no SSH passwords or private keys stored by the plugin
- file access remains inside configured roots
- symlink escapes are rejected
- Git credential/config override paths are blocked
- service control is exact-unit allowlisted
- destructive operations are audited

See `PUBLIC_RELEASE.md` and `docs/` for release details.

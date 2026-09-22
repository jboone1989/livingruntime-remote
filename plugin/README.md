# LivingRuntime Remote Plugin

Identity: `livingruntime.remote`

Current version: `0.4.13`

This directory contains the ChatGPT/Codex-facing MCP runtime and the local Connector implementation.

## Capability surface

- connection_status
- capabilities
- list_projects
- read_file
- write_file
- list_dir
- git
- exec
- process
- systemd
- logs
- apply_patch
- diagnostics

The canonical registry is `scripts/contract.py`.

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

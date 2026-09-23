# LivingRuntime Remote Plugin

Identity: `livingruntime.remote`

Current version: `0.4.20`

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
- list_credentials
- lease_credential
- list_credential_leases
- revoke_credential_lease
- create_job
- get_job
- list_jobs
- checkpoint_job
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

### Credential broker

Secrets are added only on the owned machine with `python scripts/credentialctl.py set ...`; there is intentionally no MCP tool that accepts or returns a secret value. ChatGPT sees only handles and declared capability/project/device scopes. `lease_credential` creates a short-lived opaque lease (30-900 seconds), and `revoke_credential_lease` invalidates it without deleting the underlying credential. Generic `exec_with_secret` is intentionally not provided; dedicated capabilities must consume leases internally so arbitrary commands cannot print or exfiltrate credentials.

### Durable long-running jobs

Long tasks can be represented independently of a model turn with `create_job`. The durable record stores the goal, project/device routing, optional Pi backend, current step, next action, bounded checkpoints, and terminal status. `checkpoint_job` persists resumable progress; `get_job` and `list_jobs` recover it in later turns or sessions. Existing Pi/OpenAI continuation bindings automatically adopt a durable job and checkpoint its terminal backend status.

Job state is private local control-plane data under `~/.livingruntime/jobs` by default and is written owner-only.

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

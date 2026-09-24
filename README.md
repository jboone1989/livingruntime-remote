# LivingRuntime Remote

**Durable remote execution for AI agents on machines you control.**

LivingRuntime Remote lets ChatGPT-compatible agents work on your own VPS, workstation, NAS, or other SSH-reachable machines through bounded, auditable tools. Long-running work can outlive the model turn that started it, persist checkpoints, and resume when the result is ready.

> **Give the agent a goal. Leave. Let the runtime own the wait.**

[Website](https://remote.livingruntime.com/) · [Install](https://remote.livingruntime.com/install) · [Demo](docs/DEMO.md) · [Security](SECURITY.md) · [MIT License](LICENSE)

## Why it exists

A normal AI coding loop often looks like this:

```text
you -> agent -> command/test/deploy -> long wait
                                  |
                                  +-> chat turn ends
                                      human comes back
                                      "continue"
```

LivingRuntime Remote separates the **execution lifetime** from the **conversation lifetime**:

```text
ChatGPT / Codex / another controller
                |
                v
       LivingRuntime Remote
                |
        durable job + checkpoint
                |
      your machine / your repo
                |
         task reaches a result
                |
          watcher / callback
                |
                v
       controller continues
```

That means a long task can keep running after the initiating turn ends, while the controller can later recover the durable state instead of depending on one fragile chat process.

## What you can do

- **Work on machines you already control** through a paired local Connector and SSH.
- **Run durable long-running jobs** that survive the initiating client/tool call.
- **Resume from checkpoints** instead of rebuilding task state from scratch.
- **Watch for completion without status polling** and hand the result back to a continuation-capable controller.
- **Manage multiple machines** from one Connector with explicit per-device routing.
- **Request new command capabilities dynamically** and require human approval before reuse.
- **Read/write only inside configured roots** and use allowlisted service/process operations.
- **Use secrets as capabilities, not prompt text** through opaque credential handles and short-lived leases.
- **Audit remote actions** rather than giving the model unrestricted shell access.

## The "no more continue" demo

A representative development flow is:

```text
1. Ask the agent to fix a bug and run the real test suite.
2. LivingRuntime Remote starts a detached job on your machine.
3. The conversation no longer needs to stay busy waiting.
4. The job finishes (or fails) and commits a durable receipt.
5. A watcher observes the terminal state.
6. The controller receives the result and continues from the checkpoint.
7. The loop stops only when the goal is done, blocked, or needs approval.
```

The important part is not background execution by itself. The useful primitive is:

```text
execute -> suspend -> observe completion -> wake/continue -> execute
```

See [docs/DEMO.md](docs/DEMO.md) for a reproducible walkthrough and recording script.

## Architecture

```text
AI client / controller
        |
        | OAuth-protected MCP
        v
remote.livingruntime.com
        |
        | paired device channel
        v
Local Connector
        |
        | existing key-based SSH
        v
Your host(s)
  - repositories
  - tests
  - logs
  - services
  - bounded commands
```

LivingRuntime Remote does **not** need your SSH password or private key in the public relay. SSH credentials stay in your normal local SSH configuration on the Connector machine.

## Quick start

Public users install the Connector on a computer that already has key-based SSH access to the target machine.

### Linux / macOS

```bash
curl -fsSL https://remote.livingruntime.com/install.sh | sh
```

### Windows PowerShell

```powershell
irm https://remote.livingruntime.com/install.ps1 | iex
```

The Connector asks for a one-time pairing code, the SSH host, and the workspace root that the agent is allowed to access.

The ChatGPT app/MCP endpoint is served from:

```text
https://remote.livingruntime.com/mcp
```

Directory availability depends on the platform review process; the source, relay, Connector, and MCP implementation in this repository are public.

## Durable jobs and continuation

The public MCP surface includes durable job primitives such as:

- `create_job`, `get_job`, `list_jobs`, `checkpoint_job`
- `start_pi_step`
- `watch_pi_job`, `wait_pi_job_completion`
- `bind_openai_pi_continuation`, `continue_openai_pi_job`

A durable Goal is not marked complete merely because one child command succeeds. Child work can move the Goal into a waiting or blocked state; the controller terminalizes the overall Goal only after acceptance evidence is satisfied.

This keeps "the process exited" separate from "the user's task is actually done."

## Multi-host

One Connector can route tools to multiple configured SSH hosts.

```text
list_devices()
connection_status(device="vultr")
logs(device="vultr", unit="livingruntime-remote-relay.service")
exec(device="main", argv=["ps"])
```

Permissions are host-scoped by default. A conflicting project/device selection is rejected instead of silently crossing host boundaries.

## Human approval for new commands

Commands outside the built-in development allowlist create a durable approval request containing the exact argv, target host, project, working directory, and risk class.

Approved grants can be reused and revoked later. Shells, privilege escalation, destructive filesystem commands, and direct system-service control remain hard-denied through dynamic exec authorization.

This lets the capability surface grow without turning the agent into an unrestricted remote shell.

## Credential safety

Secrets are added on the machine you own. There is intentionally no general MCP tool that accepts or returns a raw secret value.

The controller sees opaque credential references and short-lived leases. Dedicated capabilities consume those leases internally.

> **Secrets are never context. Secrets back capabilities. Models operate on capabilities.**

## Security model

The main boundaries are:

- SSH passwords and private keys are not stored by the LivingRuntime public relay.
- File operations remain inside configured roots; symlink escapes are rejected.
- Git credential/config override paths are blocked.
- Service operations are exact-unit allowlisted.
- Dynamic exec grants are explicit, auditable, scoped, and revocable.
- Connector device tokens are separate from SSH credentials.
- Public access uses OAuth and one-time device pairing.
- Destructive or privileged command classes remain denied rather than becoming approvable.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and the trust model.

## What this project is (and is not)

LivingRuntime Remote is the lightweight **remote capability and durable-execution layer**.

It owns:

- authenticated MCP access
- paired remote transport
- bounded file/Git/process/service/diagnostic tools
- multi-device routing
- dynamic execution approval
- credential capability boundaries
- durable jobs, receipts, waiting, and continuation hooks

It is **not** a hosted cloud IDE and does not require moving the whole repository into a vendor VM.

Pi Remote is a separate optional coding runtime that can use LivingRuntime Remote as transport. LivingRuntime Remote does not depend on Pi Remote.

## Repository layout

- `plugin/` — ChatGPT/Codex-facing MCP runtime and Connector implementation
- `relay/` — public OAuth relay and device pairing service
- `gateway/` — bounded MCP gateway
- `docs/` — review, deployment, demo, and architecture notes
- `.github/workflows/ci.yml` — test matrix
- `.github/workflows/release.yml` — cross-platform Connector builds

## Project status

LivingRuntime Remote is public and MIT-licensed. The codebase is under active development.

The Connector build pipeline targets:

- Windows x64
- Linux x64
- Linux ARM64
- macOS Intel
- macOS Apple Silicon

OpenAI app submission material and review-specific status live under `docs/`. Repository source state and directory-listing state are intentionally treated as separate things.

## Contributing

Issues and pull requests are welcome. Before changing a security boundary, remote capability, or public tool contract, please describe the threat model and expected failure behavior as part of the change.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).

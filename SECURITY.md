# Security Policy

LivingRuntime Remote is a remote-execution capability layer. Security boundaries are part of the product contract, not optional hardening.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a vulnerability that could expose credentials, bypass configured roots, escape an approval boundary, or execute unintended commands.

Use the private support channel at:

https://remote.livingruntime.com/support

Include:

- the affected version or commit;
- the capability/tool involved;
- the expected boundary;
- the observed behavior;
- minimal reproduction steps;
- whether you believe secrets, filesystem boundaries, command authorization, OAuth, pairing, or cross-host routing are affected.

Do not include real production credentials in the report.

## Security model

LivingRuntime Remote is designed around bounded capabilities rather than an unrestricted remote shell.

Key boundaries include:

- SSH passwords and private keys remain in the user's normal SSH environment on the Connector machine.
- The public relay does not need to store SSH private keys.
- File access is restricted to configured workspace roots.
- Symlink escapes are rejected.
- Git credential/config override paths are blocked.
- Service operations are exact-unit allowlisted.
- Dynamic exec approvals are explicit, scoped, durable, auditable, and revocable.
- Hard-denied command classes cannot become allowed merely through dynamic approval.
- Non-interactive sudo supports a small administrative executable set and always needs an exact,
  revocable host/project/cwd grant. It never receives an all-host diagnostic grant, changes sudoers,
  or prompts for or stores a password. The host's existing sudo policy remains authoritative.
- Credential values are not general model inputs; controllers receive opaque references/leases.
- Device/project routing is explicit and conflicting selections fail closed.
- Public access is protected by OAuth and one-time device pairing.
- Secure interactive terminals require an exact host/project/cwd/argv grant, even for
  otherwise built-in commands. They are capped at four concurrent sessions and five
  minutes each; an expired session terminates its process group.
- The secure terminal widget performs ephemeral P-256 ECDH, HKDF-SHA256 and AES-256-GCM
  in the browser. Terminal stdin and output are encrypted before MCP/relay calls;
  the Connector decrypts stdin in memory and disables PTY echo before starting the
  approved process. No terminal plaintext is included in audit events or durable
  relay task payloads. The connector requires a strictly increasing input counter,
  authenticated ciphertext, and a fresh one-time client key binding.
- A separate secure-input widget supports masked single-line and multiline
  credentials. Each three-minute form is bound to a handle, capability, project,
  and device before entering any secret. It is consumed at first submission,
  and only opaque handle/scope metadata returns through MCP.

## Trust boundary

The Connector runs on a machine controlled by the user and relies on that machine's existing SSH configuration to reach target hosts.

LivingRuntime Remote reduces the amount of authority exposed to an AI controller. It does not turn hostile target code into a safe workload by itself, and it should not be treated as an OS sandbox for arbitrary untrusted code.

Run the Connector and target-side development services with the least operating-system privilege that satisfies the intended workflow.

## Supported versions

The project is under active development. Security fixes are applied to the current `main` line and current public release path; old development snapshots should not be assumed to receive backports.

## Secrets

Never paste SSH private keys, passwords, provider API keys, or other long-lived credentials into a GitHub issue, demo recording, model prompt, or repository file.

When adding a new secret-consuming capability, prefer a dedicated capability that consumes a bounded credential lease internally rather than a generic command that receives the raw secret.

## Secure terminal limitations

- The browser-to-Connector ECDH exchange is authenticated by the existing OAuth
  and device-pairing transport, not by an independently pinned Connector public
  identity. An actively malicious/compromised relay or web app host can substitute
  keys or script content. This protects against accidental transcripts and passive
  relay storage, **not** a fully untrusted relay.
- A command can itself echo, print, persist, or forward an entered secret. The
  Connector disables initial PTY echo, but cannot control a program that later
  reenables echo or logs stdin. Only type secrets into programs you trust.
- The first version supports POSIX Connectors and SSH to configured hosts,
  not native Windows PTYs. The terminal does not grant shell privileges:
  exact command approvals and existing OS authorization still apply.
- Do not include credentials in the initial command argv, workspace files,
  approval requests, issue descriptions, or ordinary chat messages.
- The isolated vault runs as a **different Linux UID** (`livingruntime-vault`)
  from the Connector/remote command executor. Vault state is held in 0700
  directories with owner-only credential files, and the Unix socket requires
  an exact Connector UID through `SO_PEERCRED`; no raw-secret-resolution
  RPC exists. A process under the Connector UID can still request the
  broker's finite allowlisted operations, such as named provider identity
  checks. All invalid requests fail closed and no plaintext is returned.
- The broker's credential files are **not encrypted at rest**. Root,
  processes with the vault UID, or privileged local backups can read them.
  The daemon's source and Python environment must be root-owned and not
  writable by Connector-side executors. The installer copies broker code
  outside the user's home rather than directly importing code from a
  development worktree.
- Ordinary Remote read/write/list/patch tools additionally deny the old
  user credential directory to reduce accidental exposure. This is not a
  sandbox; an authorized same-UID command can read **other** files owned by
  that UID, so vault state must never be under the Connector account.
  Earlier legacy files are not automatically migrated, and old copies
  must be retired after rotation.
- Authentication handoff and resume use the host-provided
  `sendFollowUpMessage` UI API after a confirmed terminal or vault
  receipt; it requests the agent to continue the interrupted work but
  cannot guarantee that a separately running job resumes autonomously.

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

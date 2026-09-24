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
- Credential values are not general model inputs; controllers receive opaque references/leases.
- Device/project routing is explicit and conflicting selections fail closed.
- Public access is protected by OAuth and one-time device pairing.

## Trust boundary

The Connector runs on a machine controlled by the user and relies on that machine's existing SSH configuration to reach target hosts.

LivingRuntime Remote reduces the amount of authority exposed to an AI controller. It does not turn hostile target code into a safe workload by itself, and it should not be treated as an OS sandbox for arbitrary untrusted code.

Run the Connector and target-side development services with the least operating-system privilege that satisfies the intended workflow.

## Supported versions

The project is under active development. Security fixes are applied to the current `main` line and current public release path; old development snapshots should not be assumed to receive backports.

## Secrets

Never paste SSH private keys, passwords, provider API keys, or other long-lived credentials into a GitHub issue, demo recording, model prompt, or repository file.

When adding a new secret-consuming capability, prefer a dedicated capability that consumes a bounded credential lease internally rather than a generic command that receives the raw secret.

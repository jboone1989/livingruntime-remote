# LivingRuntime Remote — Web Control Plane decision (2026-10-15)

## Decision

Build a **thin, mobile-friendly Web/PWA companion** over the existing authenticated Remote relay and MCP APIs, but do **not** build a standalone public multi-tenant SaaS or another AI coding agent yet. Validate value with current users and opt-in self-hosters before hosting third-party infrastructure.

## User problem and differentiation

Single-vendor remote control is increasingly native to Claude Code, Codex, and other providers. A browser that merely mirrors one terminal/session has weak differentiation. LivingRuntime's plausible advantage is **vendor-neutral operations** across heterogeneous agents and machines:

1. One view of direct Remote operations and *optional* Claude/Codex/other Agent adapters.
2. Evidence-backed durable jobs: source, host, project, process status, audit trail, checkpoints, completion receipt and explicit acknowledgement.
3. Cross-host dispatch with project-scoped approvals, local credentials and fail-closed policy.
4. Review and intervention on phone without keeping a desktop session open.

Competing references to watch: Anthropic Claude Code Remote Control, OpenAI Codex Remote, amux, ClauBoard and Flightdeck. They already cover significant parts of the UI/control-plane problem; do not claim novelty of a generic dashboard.

## Product boundary

The **Connector** and Agent Adapters run on user-controlled machines. The **Relay** authenticates and routes commands/events. The **Web Control Plane** reads the same authorization-scoped state. It is neither an LLM provider, nor a remote terminal exposed to the public internet, nor a replacement for the Host MCP app.

Minimum usable Web UI:

- **Tasks:** one timeline for direct and delegated tasks; honest running/waiting/blocked/terminal status, result receipts, filters by project/device.
- **Agents:** list installed adapters and allowed workspaces; launch in initially non-destructive/plan mode; resume/send/cancel with explicit scoped authorization.
- **Hosts:** health, configured workspaces, service status, connectivity.
- **Approvals:** inspect *exact* execution request with scoped approve/deny/revoke; never bulk-approve unseen commands.
- **Events:** unacknowledged completions, subscription availability, delivery failure; distinguish saved event from ChatGPT/other host actually receiving it.

Retain the existing MCP App as the embedded lightweight interface; factor out shared UI components/API contracts rather than maintaining two unrelated dashboards.

## Open access & trust requirements

Public landing page may be public; each operator's dashboard must require a strong logged-in identity and per-user ownership on every resource. Pairing is one-time and user-initiated; minimum scopes, device revocation, credential isolation and audit trails. No public anonymous access to machines, arbitrary shells, filesystem roots, raw secrets, or logs. Rate-limit approvals/dispatch and protect against cross-tenant routing, replay, CSRF and SSRF. Verify source of callback events. Do not expose the user's machines by inbound public ports.

Begin with read-only dashboard and a small closed beta. Do not accept public tenants until tenancy-isolation tests, threat review, onboarding/offboarding and rollback have passed.

## Adoption decision criteria

- At least 5 independent users actively connect a real machine and complete tasks for 2 weeks.
- At least 3 need two different Agent vendors, or multiple hosts, beyond their current single-vendor tools.
- They routinely use recovery, approval or cross-agent comparison rather than only viewing a pretty status screen.
- Credential/token hosting and support costs fit the low-cost operational budget.

If these are not true, keep it an open-source/self-hosted utility plus ChatGPT Plugin rather than investing in a paid public SaaS.

## Iteration

1. Improve embedded MCP dashboard grouping; do not fake agent live status.
2. Reuse relay authenticated endpoints and add an operator-only responsive standalone view (local or self-hosted first).
3. Validate with users; only then consider multi-tenant hosted accounts, billing, onboarding, team policies and paid support.

## Monetization hypothesis

Open-source Connector + free self-hosted dashboard; optional hosted relay/monitoring, multi-user permissions, longer audit retention and managed operations on subscription. Charge for reliable coordination and operational convenience, **not for another wrapper around Claude/Codex**. No payment tiers before users prove willingness to pay.

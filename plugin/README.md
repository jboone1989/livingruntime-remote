# LivingRuntime Remote Plugin

Identity: `livingruntime.remote`

Current version: `0.4.65`
Current version: `0.4.57`

This directory contains the ChatGPT/Codex-facing MCP runtime and the local Connector implementation.

## Capability surface

- connection_status
- capabilities
- list_devices
- open_remote_control_plane
- remote_overview
- list_projects
- read_file
- write_file
- list_dir
- git
- exec
- terminal_start (model-visible, exact-grant interactive PTY setup)
- terminal_key_exchange / terminal_input / terminal_read / terminal_close (app-only, encrypted)
- list_credentials
- lease_credential
- list_credential_leases
- revoke_credential_lease
- github_identity
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
- start_pi_agent
- start_pi_step
- watch_pi_job
- wait_pi_job_completion
- bind_openai_pi_continuation

The canonical connector-forwarded registry is `scripts/contract.py`. Relay-only
ChatGPT Apps entrypoints such as `open_remote_control_plane` are registered in `relay/server.py`.

### Secure interactive terminal

Use the model-visible `terminal_start` tool to nominate an exact
`argv`, `project`, and optional `device`/`cwd`, for example
`["claude", "login"]`. If an approval is returned, explicitly approve
that exact interactive command via the existing permission flow, then
invoke `terminal_start` again. The ChatGPT app opens an embedded terminal
panel. **Type passwords, OTPs, or authorization codes in that panel only.**
Do not pass them in `argv`, tool arguments, or chat messages.

The panel establishes a fresh P-256 ECDH/AES-256-GCM session directly with
the Connector over the relay, and all terminal input and output are
encrypted before being sent through MCP. Relay task receipts contain only
ciphertext. The panel disables input echo, does not write credential text to
widget state, and offers a Close button. Session lifetime is at most 300
seconds with automatic process cleanup; maximum four concurrent sessions
per Connector. The exact-command grant remains subject to normal Remote
revocation and OS policy. Initial support requires a POSIX Connector;
Windows native PTY support is not included. The relay and browser host
remain trusted for active key exchange and app integrity (see SECURITY.md).

Run targeted tests with:

```bash
python -m unittest discover -s plugin/tests -p 'test_secure_terminal*.py'
```

### Credential broker

Secrets are added only on the owned machine with `python scripts/credentialctl.py set ...`; there is intentionally no MCP tool that accepts or returns a secret value. ChatGPT sees only handles and declared capability/project/device scopes. `lease_credential` creates a short-lived opaque lease (30-900 seconds), and `revoke_credential_lease` invalidates it without deleting the underlying credential. Generic `exec_with_secret` is intentionally not provided; dedicated capabilities must consume leases internally so arbitrary commands cannot print or exfiltrate credentials.

`github_identity` is the first dedicated lease consumer. It accepts only a `github.identity` lease for provider `github`, sends it only to the fixed `https://api.github.com/user` endpoint with redirects disabled, and returns only public account identity fields.

### Control plane and host discovery

`open_remote_control_plane` is the explicit ChatGPT Apps SDK entrypoint for the live dashboard. It opens the current control-plane widget with resource inventory enabled so a user request such as “open Remote Control Plane” maps to a UI action instead of a data-only overview call.

`remote_overview` remains the read-only data API behind the dashboard. It combines paired-connector health, configured host reachability, durable jobs, sanitized pending approvals, credential handles, active leases, recent tool activity, and a conservative `execution` receipt. The execution receipt distinguishes real server-side work (`RUNNING`, only with a live worker/child process), cognition waits, recent completed tool activity, stalls, and `IDLE`; dashboard refreshes are excluded so the widget cannot manufacture apparent work. When no durable work is running and the last real activity is old, it explicitly warns that an older ChatGPT tool timeline may be stale. It intentionally does not expose full pending command argv; operators inspect those through `list_exec_permissions` before approval.

Watcher lifecycle is intentionally separate from execution truth: `ARMED` means the ChatGPT widget is attached and waiting, `WAITING_FOR_CHATGPT_SESSION` means a non-terminal durable result or cognition request is ready for ChatGPT, `COMPLETED` means an authoritative terminal receipt has already closed the watcher, and `DISCONNECTED` means the widget is gone or its transport is unavailable. Destroying a widget never leaves a watcher looking `RUNNING`; server `RUNNING` is reserved for receipts with an observed live worker/child process. Reopening the same conversation preserves the watcher identity so an already-claimed durable cognition request can be handed to the restored ChatGPT session immediately instead of waiting for the claim lease to expire.

`list_devices(include_resources=true)` adds bounded live inventory for each configured host: CPU count/load, available memory, disk usage for configured roots, project presence, and allowlisted service state. Resource discovery never scans arbitrary network hosts.

### Durable long-running jobs

Long tasks can be represented independently of a model turn with `create_job`. The durable record stores the goal, project/device routing, optional Pi backend, current step, next action, bounded checkpoints, and terminal status. `checkpoint_job` persists resumable progress; `get_job` and `list_jobs` recover it in later turns or sessions. Existing Pi/OpenAI continuation bindings automatically adopt a durable job and checkpoint its terminal backend status.

Job state is private local control-plane data under `~/.livingruntime/jobs` by default and is written owner-only.

`start_pi_agent` is the preferred development path. It sends the goal into a normal Pi API-mode AgentSession and starts a detached `prompt-api` job. Pi remains the orchestrator and uses `livingruntime-chatgpt/chatgpt-web` as an ordinary custom model provider. Provider calls are bridged through the durable cognition queue; ChatGPT returns text or structured Pi tool calls, while Pi itself executes tools and decides when its native loop is complete.

`start_pi_step` is retained for deterministic external-controller work where Pi model calls must remain zero.

On the external-controller compatibility path, ChatGPT supplies a bounded batch of structured Pi actions; Pi executes the batch in a detached worker while making zero model calls. The first step creates a durable Goal and an external-controller Pi session. Later steps pass the same `runtime_job_id`; the Goal reuses the same Pi session while each child Pi job is replaceable.

A child Pi step reaching `SUCCEEDED` moves the durable Goal to `WAITING`, not `SUCCEEDED`. A failed or cancelled child moves the Goal to `BLOCKED`. Only the controller may terminalize the overall Goal through `checkpoint_job` after acceptance evidence is satisfied. Detached `external-actions` reject Pi `bash`; commands and tests continue through the normal Remote `exec` permission layer.

### Dynamic exec approvals

`exec` keeps the built-in bounded development command set, but an otherwise unlisted executable
now produces a durable approval request instead of requiring a code change. Approval is exact-command
and host-scoped by default. Read-only diagnostics may be approved for all owned hosts or as a
diagnostic class. Non-interactive `sudo -n COMMAND ...` (or `--non-interactive`) is supported for
Docker, apt/apt-get/apt-cache, dpkg, nginx, caddy and identity checks. Every sudo command requires
an exact host/project/cwd grant, including identity checks; the same approved command can be reused
until revoked. Interactive sudo, sudo option overrides, shells, interpreter/wrapper targets,
destructive filesystem commands and direct system service control remain denied.

The Connector does not edit sudoers or store a sudo password. The Unix account must already be
authorized by the host's sudo policy; a password-required command fails promptly rather than
waiting for terminal input. Approved sudo commands also work through durable long jobs.

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

On Windows, the default installer asks for the pairing code and a bounded local workspace such as `D:\\Projects`; it uses the native local executor and does not require localhost SSH. On Linux/macOS, or when Windows is explicitly configured with an SSH host, the Connector uses key-based SSH. Pairing codes are created by an authenticated LivingRuntime Remote user in ChatGPT.

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

## Runtime boundary after DevDay 2026

LivingRuntime Remote is an execution substrate, not a second general-purpose agent framework.

- OpenAI runtime owns model reasoning, Work/event activation, generic agent session behavior, and plugin discovery.
- LivingRuntime Remote owns remote/local execution, bounded workspaces, multi-host routing, process/service lifecycle, durable jobs, logs, Git/filesystem operations, and execution receipts.
- Higher-level agents such as Ferro own identity, internal state, goals, context selection, experience/outcome learning, and self-improvement policy.

The portable package exposes independently discoverable skills:
- `remote-development` for shared execution, routing, permissions, and repository rules.
- `debug-and-repair` for fault diagnosis and capability-preserving repair.
- `deploy` for validated main/release/deployment workflows.
- `long-job-recovery` for durable job liveness and completion recovery.

The public relay already serves Control Plane and watcher UIs as MCP Apps resources (`text/html;profile=mcp-app`). `remote_overview` is intentionally data-only; only `open_remote_control_plane` attaches the Control Plane UI resource. This keeps refresh/data calls headless and prevents unnecessary iframe re-renders while still allowing the UI to call data tools through the MCP Apps bridge.

The root `.app.json` is a registered-MCP-server mapping for local/workspace packaging, not the MCP Apps UI manifest. It remains empty until an eligible registered server ID is available; UI resources are declared by the MCP server itself.

The plugin lifecycle intentionally does not contain a blocking `Stop` hook. Long-job/cognition completion is durable and is recovered from authoritative receipts on activation or session resume rather than keeping a completed model turn open.


## Completion receipts

Version 0.4.61 does not automatically acknowledge terminal long-job receipts during get/watch/wait. A separate explicit ACK is required after the consumer processes the completion event. Ordinary unclaimed completions expire after the configured retention interval (24 hours by default), but delegated `agent-adapter` completion events remain unacknowledged until an explicit ACK. Durable job history is retained. The Control Plane reports completion receipts separately from tasks that are actually waiting to run or resume.

## MCP Events

Version 0.4.57 makes native MCP Events the sole proactive activation path. `watch_agent_cognition` verifies the authenticated user's unexpired `cognition.requested` subscription for the exact agent; it neither subscribes nor polls nor claims. The Work host must register `events/subscribe` with its callback and secret. `SUBSCRIPTION_REQUIRED` is an explicit missing registration, not an armed watcher. Events provide only request identity/routing metadata; fetch authoritative messages with `get_llm_request`, stop for `COMPLETED` or `TIMED_OUT`, and claim Ferro requests with `chatgpt-work-ferro-cortex` for 300 seconds. Completion immediately claims an available request only in the completed request's lane. A returned auto-claim can be reused; a live claim belonging to another watcher is a conflict.
The relay advertises `capabilities.events` on MCP 2.0 discovery and implements
`events/list`, `events/subscribe`, and `events/unsubscribe` on the authenticated
MCP endpoint. Subscriptions are durable in the relay store, callback URLs are HTTPS-only
and resolved to public addresses before a pinned TLS connection is opened, callback
ownership is verified with a signed challenge, and deliveries use Standard Webhooks
headers with bounded retry behavior. The Connector forwards one durable completion
event ID to the relay and records successful relay acceptance so ChatGPT-side polling
is not required for terminal long-job notification.

Widgets are status surfaces only: they never inject `ui/message` or `sendFollowUpMessage` prompts. GitHub wake bridging has been removed. Durable MCP Events handle proactive activation, while lifecycle hooks recover already-bound durable state when a session starts, resumes, compacts, or receives a user prompt.

## Optional Agent Adapters (0.4.65)

Remote functions with zero installed agents. Adapter packages are optional and independently installed; agent-specific runtime code must not enter Remote Core. `list_agents` reports adapters; `start_agent`, `get_agent_session`, `send_agent_message`, `resume_agent`, `watch_agent_events`, and `cancel_agent` operate on scoped sessions.

Use `python3 plugin/scripts/adapterctl.py install livingruntime_adapter_http make` (or `uninstall`) to manage the explicit adapter manifest. The optional HTTP adapter connects only to a loopback agent service configured by `LIVINGRUNTIME_HTTP_AGENT_URL` and `LIVINGRUNTIME_HTTP_AGENT_TOKEN`. Actual agent software must run independently and implement its session protocol; no LLM or agent is bundled with Remote.

Agent launches are disabled unless the connector environment explicitly specifies a matching `LIVINGRUNTIME_AGENT_LAUNCH_ALLOWLIST` entry of `device:project:adapter_id`. Restart after installation or environment changes. Direct file/Git/exec operation remains available if no agent is installed or an adapter fails to load. Durable Job receipts require explicit ACK, and ChatGPT autonomous wake-up still requires an authenticated host-side event subscription.

### Claude Code adapter (optional, 0.4.65)

VM09 has Claude Code installed. The optional `livingruntime_adapter_claude` starts read-only planning tasks in a detached worker with owner-only persisted state and result receipts. It supports fresh sessions, follow-up prompts, explicit cancellation and status retrieval even after a connector restart. Its initial mode is strictly `--permission-mode plan --max-turns 1`, not autonomous edits or bypassPermissions. No native event push or host automatic wake-up is available; consumers should query `get_agent_session` or use the durable Job receipt path.

Install by registering `livingruntime_adapter_claude` with `adapterctl.py install livingruntime_adapter_claude make`. Agent launch is **denied by default**. An operator-owned, mode-0600 file `~/.livingruntime/agent-policy.json` may specify `launch_allowlist` entries of `device:project:adapter_id` and `claude_workspaces` entries of `device:project` to an existing directory. Environment allowlists remain supported. The adapter is optional: no Agent is needed for regular Remote use. If the Agent fails, direct Remote tools remain available, without silently inheriting Agent privileges.

### Periodic agent reconciliation (0.4.65)

The one-shot `agent_reconciler.py` inspects persisted Agent Sessions and updates nonterminal Durable Jobs from actual adapter status. Run under a separate user-level timer if autonomous Job recording is required. It does not ACK completion events, claim external host subscriptions, or grant Agent execution permissions. No-Agent mode exits without error. Host-side ChatGPT wake-up still requires an authenticated host event subscriber.

### Reconciliation fix (0.4.65)

The standalone systemd reconciler loads explicit adapter registrations before inspecting persisted sessions. Without this, the connector could list Claude Code while the background reconciler failed to map its durable jobs. Verified using a real delegated Claude Code job: the background one-shot reconcile completed with updated=1, and the durable completion event remained PENDING until ACK.

### Control Plane presentation

The embedded MCP App now separates delegated Agent jobs from directly executed Remote jobs. The UI shows active delegated work without claiming that an agent process is live unless a backend liveness receipt supports it. Completion receipts remain distinct from pending work and do not indicate automatic ChatGPT host wake-up. The UI is a status and review surface, not a browser shell or a new Agent implementation. A standalone multi-tenant Web/SaaS console is considered separately in `docs/WEB_CONTROL_PLANE.md`.
### Bounded file deletion

`delete_path` removes a single literal POSIX path inside configured roots. Directories require `recursive=true`; a leaf symlink is unlinked without following its target. `dry_run=true` reports the planned entry/byte count without deleting. `expected_sha256` protects regular files against stale content. Missing unguarded paths return `NOT_FOUND`.

Configured roots, configured project roots and their ancestors cannot be removed. Home credential and Connector state directories are protected recursively. Parent symlinks, traversal segments, special files, other filesystems and trees above 10,000 entries, 512 MiB or 64 levels are refused. Partial failures report actual deletion counts and keep an audit record; directory deletion is not transactional. Native Windows deletion is currently unsupported and fails closed. Generic shell and deletion commands remain hard-denied through exec.

Installer 0.4.65: handles inaccessible inherited working directories and automatically sets valid user systemd session environment for linger-enabled service users. Administrator linger authorization remains required where policy restricts it.

## Install without pairing (experimental)

On Linux or macOS, run the version-pinned one-command installer:

```sh
curl -fsSL https://raw.githubusercontent.com/jboone1989/livingruntime-remote/main/plugin/scripts/install-connector.sh | sh
```

This puts the native binary at `~/.local/bin/livingruntime-remote`. Review
remote scripts before running; include `~/.local/bin` in PATH. Pair separately:

```sh
livingruntime-remote install --local --root "$HOME/workspace" --pair YOUR_CODE
```

The npm wrapper in `packages/npm/` is a prototype marked private and is
not published to npm. Do not use `npm install -g @livingruntime/remote` yet.

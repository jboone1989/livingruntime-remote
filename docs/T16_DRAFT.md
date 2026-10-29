# T16 isolated draft implementation

**Experimental code, disabled by default. Do not merge or deploy.**
The owner explicitly authorized an isolated code experiment while the product
Spec remains unapproved. This does not approve Spec #79 or its full Phase 1.
Implementation baseline: upstream main `f39b24349c62b52c444f9cbe07fa15739f81d69f`.
Spec reference: [#79 at d2b82cd](https://github.com/jboone1989/livingruntime-remote/blob/d2b82cd965c24dd31fdc80b0ef0608cd225d9c45/specs/agent-workboard/spec.md).
An approved fixed Spec revision, review, full CI and isolated real-host acceptance
are required before any merge or deployment. VM09, production and EventGraph
worktrees and their existing dispatchers were not used or changed.

## Existing implementation inspected

- `relay/store.py`: device identity, SQLite WAL, RPC tasks. Workboard uses
  **the same RelayStore database connection factory**, separate prefixed tables;
  no change to existing RPC task retention or claim semantics.
- `plugin/scripts/relay_agent.py`: existing persistent polling service and executor.
  A bounded placement tick runs on that executor, not a new timer/service/framework.
- `agent_adapters.py`, `agent_sessions.py`, `jobs.py`: existing native session
  wrappers and durable terminal receipts are reused, including unacknowledged
  completion events. A WorkItem attempt references its session/runtime job.
- `bridge.start_agent`, `agent_policy.py`: existing host/project/adapter launch
  policy remains the launch gate; `bridge.exec` remains the exact-command
  permission gate for bounded acceptance checks.
- `claude_mobile.py`, `claude_pr_review.py`, `claude_issue_review.py`, and
  `bridge.github_identity`: existing native/GitHub handoff and review paths.
  There is no replacement GitHub credential store, PR writer or CI runner here.
- SQLite [transaction rules](https://www.sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions):
  short `BEGIN IMMEDIATE` transactions serialize dependency/resource checks and
  lease/fence allocation. No network call runs inside a transaction.

## Implemented draft slice

- Exact owner + paired Connector UUID + operator-selected `agent_id` ownership,
  with a fixed adapter/project/local-host placement. Agent IDs are logical slots
  within a trusted Connector, not independently authenticated principals.
- Immutable goal text, source commit/content hash, optional fixed acceptance argv,
  idempotent creation with conflict rejection, existing-item dependencies.
- Atomic claim, 180-second lease, monotonic attempt/fence, heartbeat and stale
  update rejection, one active claim per slot and at most two per Connector.
- Owner-scoped resource exclusion, including verification/human waits. Different
  exclusive keys permit parallel independent work; keys must identify the actual
  worktree/resource consistently across machines and Agents.
- Human requests bind attempt/fence/source revision; one resolution, no replay.
  The operator must reconcile native execution and external effects before retry.
  A retry is fresh authorization for the next attempt of this fixed item, not a
  native CLI permission approval carried across fences. Three attempts maximum.
- Expired/ambiguous native execution is retained for human reconciliation, never
  automatically relaunched merely because a timer elapsed. Resource stays held.
- Persistent, owner-only, write-ahead launch journals and process file locks;
  restart reattachment, lost attachment/finish responses, archived receipts.
- Native success with no automatic check enters VERIFYING. Owner verification
  requires evidence and the exact fence/revision. With a repeatable acceptance argv,
  the worker runs the existing bounded exec path and submits checked evidence;
  successful acceptance releases dependent work automatically. Missing command
  approval never becomes an implicit grant. A check-only retry reuses the finished
  native session and job instead of repeating its external effects.
- On finishing or blocking one item, claim the next independent item in the same
  tick. Dependencies require SUCCEEDED; failed predecessors create human requests.
  Durable completion ACK does not gate dispatch. Idle tick cadence follows the
  existing Connector poll (normally up to 20 seconds plus request latency).

## Local isolated experiment configuration

Do not enable on production. In an isolated Relay and test Connector process only,
set `LIVINGRUNTIME_WORKBOARD_DRAFT=1` on both. The existing Connector pairing JSON
may specify up to two explicit placements:

```json
{"workboard_workers": [{"agent_id": "local-codex", "adapter_id": "codex",
                       "project": "scratch", "local_device": "main"}]}
```

Existing MCP OAuth `remote:write` gates `workboard_create`, `workboard_resolve`,
`workboard_verify`; `remote:read` gates `workboard_list`. Device operations use the
existing paired device bearer token at `/device/workboard`. No caller-supplied
owner/device ID is trusted on that device route. All new paths fail closed when
the draft flag is absent. Only explicit placements participate; no machine is
discovered or contacted by this patch.

## Validation and limits

Focused standard-library tests run locally on Python 3.14 / SQLite 3.46.1:

```sh
cd relay
python3 -m unittest -q test_workboard test_store
cd ..
python3 -m unittest discover -s plugin/tests -p 'test_jobs.py'
python3 -m unittest discover -s plugin/tests -p 'test_agent_sessions.py'
python3 -m unittest discover -s plugin/tests -p 'test_agent_adapters.py'
```

This VM lacks pip/ensurepip, MCP and Starlette, and terminal networking is denied.
Full Relay HTTP/MCP registration, Connector polling regression suites, Windows
file locks, native Agent execution and two real-host/offline acceptance remain
**unverified locally**. Pure coordination tests use two paired device identities,
concurrent independent SQLite connections and real existing session/job stores
with a test native adapter; these are not real-host acceptance claims.

This is not full Phase 0/1: no Goal UI, budgets, capability negotiation, dynamic
eligibility, automatic subtask generation or native permission-decision bridge.
Automatic checks must be owner-authored, bounded, repeatable acceptance commands;
they cannot prove subjective requirements or bypass CI/review/merge gates.
Native CLI tools are not sandboxed or individually fenced by Remote. Therefore
this draft cannot promise exactly-once PR creation/push/deployment or stop an
offline native process from making external writes. It intentionally keeps
ambiguous resources blocked until process/effect reconciliation. Do not enable
for unattended external side effects until that adapter boundary is verified.

Spec #79 still has unresolved approval-resume and PROPOSED/HumanAction semantics;
this experiment makes no PROPOSED transition and implements explicit retry
authorization rather than pretending an old permission approval is transferable.

## Source acquisition and portable artifacts

Git/gh terminal access failed (DNS/socket restrictions); the authenticated GitHub
connector supplied all 197 text files at the pinned upstream revision. Their
original blob hashes were checked before modification. The connector cannot
decode `plugin/assets/logo.png`; it was not substituted. Consequently the local
Git repository has a clearly labeled **text-snapshot base commit**, not upstream
history. Its standalone bundle contains that snapshot and the draft changes.
The patch applies onto the real upstream revision; a GitHub draft PR built from
the original upstream tree preserves the binary asset and upstream parent.

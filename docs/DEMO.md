# Demo: stop typing "continue"

This demo shows the product behavior LivingRuntime Remote is designed to enable:

> A long-running task keeps working on a machine you control after the initiating model turn ends, produces a durable result, and gives a continuation-capable controller enough state to resume the goal without a human status-polling loop.

## What this demo proves

It is intentionally stricter than "run a command in the background."

A successful demo should show all of these:

1. **The task outlives the initiating turn.**
2. **The remote machine owns the running process**, not the browser tab.
3. **Job state is durable** and can be recovered by ID.
4. **Completion is observed without repeated status polling.**
5. **The next controller turn receives the terminal result/checkpoint.**
6. **The goal continues from that result** instead of asking the user to type "continue."
7. **Approval boundaries still stop the loop** when a new risky capability is required.

## Suggested real task

Use a repository with a test suite and a deliberately failing test.

A good prompt is:

```text
Fix the failing test in this repository and run the relevant test suite.
If a long-running step outlives this turn, use LivingRuntime Remote's
durable job + watcher path. Continue from the completion result without
asking me to poll for status. Stop if you need a new approval or if the
acceptance test cannot be satisfied safely.
```

This is better than a synthetic sleep command because it exercises a real development loop.

## Expected flow

```text
User goal
  |
  v
Controller
  |
  | create/adopt durable Goal
  v
LivingRuntime Remote
  |
  | start detached child work
  v
User-owned host
  |
  | tests / edits / checks
  v
Durable terminal receipt
  |
  | watcher observes terminal state
  v
Controller continuation
  |
  | read checkpoint + evidence
  v
Next bounded action
```

## Relevant public tools

The exact controller path may vary, but the public capability surface includes:

- `create_job`
- `get_job`
- `list_jobs`
- `checkpoint_job`
- `start_pi_step`
- `watch_pi_job`
- `wait_pi_job_completion`
- `bind_openai_pi_continuation`
- `continue_openai_pi_job`

The implementation is intentionally split between durable goal truth and replaceable child work. A child process exiting successfully does not automatically mean the user's goal is complete.

## Recording checklist

For a short public demo, capture only these moments:

1. The initial user request.
2. The durable job starting and returning control to the conversation.
3. The user doing nothing while the remote task continues.
4. The completion/watcher event.
5. The controller continuing automatically from the checkpoint.
6. The final acceptance evidence (for example, tests passing).
7. Optionally, one approval stop to show that autonomy is bounded.

Keep the recording focused. The core story should be understandable in under a minute:

> "I gave ChatGPT a real task on my server, left it alone, and it came back with the next step when the long-running work finished."

## Failure cases worth demonstrating

A serious remote runtime should make failure visible instead of pretending progress.

Useful negative demos include:

- Connector goes offline.
- Child worker exits unexpectedly.
- A command requires a new approval.
- A task reaches `BLOCKED`.
- A watcher reconnects after the job already reached a terminal state.
- A later turn recovers the same durable job by ID.

## Safety expectation

The demo should not weaken any normal safety boundary for presentation purposes.

In particular:

- do not paste SSH private keys or passwords into model context;
- do not grant an unrestricted shell just to make the demo easier;
- keep the repository inside a configured root;
- use exact or bounded command permissions;
- let the workflow stop when human approval is required.

The point is to demonstrate **useful continuity without giving up control**.

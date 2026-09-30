---
name: long-job-recovery
description: Recover or diagnose LivingRuntime durable long jobs whose watcher disconnected, stalled, lost heartbeat, or has a completion awaiting delivery.
---

Treat the durable job receipt as authoritative. Start with `get_long_job(job_id)`; do not infer liveness from the ChatGPT thinking indicator, an old watcher card, or elapsed wall time.

If the receipt is terminal, deliver or acknowledge completion through the dedicated long-job completion tools when applicable, then stop waiting. If it is non-terminal, compare heartbeat/progress timestamps with actual worker/child liveness before calling it running.

For `STALLED`, `ORPHANED`, `HEARTBEAT_STALE`, `LOST`, or transport disconnection, inspect the host/process and logs needed to distinguish a live worker from a dead job. Surface the state explicitly. Do not manufacture progress and do not restart work merely because the UI detached.

The plugin deliberately does not use a blocking Stop hook. Session resume and user-prompt hooks may recover durable bindings, but they must not keep a finished model turn open while waiting for future work.

Cancel only when the user requested cancellation or the job's own recovery policy requires it.

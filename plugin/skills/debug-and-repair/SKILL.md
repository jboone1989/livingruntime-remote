---
name: debug-and-repair
description: Diagnose and repair broken remote development capabilities, regressions, service failures, and fail-silent behavior using LivingRuntime Remote.
---

Use LivingRuntime Remote as the execution substrate. Resolve the target with `list_projects` and, when host selection matters, `list_devices` / `connection_status`. Inspect repository branch/status and the smallest relevant logs or files before editing.

Do not hide the failure with broad exception catches, silent downgrade paths, shadow-only behavior, compatibility branches, or no-op fallbacks. Repair the broken capability chain so the normal path works and the original failure becomes observable if it regresses.

Preserve unrelated working-tree changes. Never reset, clean, force-push, or overwrite concurrent work to simplify the repair.

For code edits, read the target first and prefer `apply_patch` guarded by the returned SHA-256. Use `write_file` for new small files. Run focused tests covering the changed path first and add a regression test for the observed defect when practical.

If a command can exceed a normal model turn, use `start_long_job` and durable receipts. A disconnected watcher is not proof of a running process; inspect `get_long_job` and actual host/process state.

Finish by inspecting the diff and repository status. If the user asked for the repair to ship, continue with the `deploy` skill rather than leaving validated code unmerged.

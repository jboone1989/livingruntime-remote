# Debug and repair workflow

Use the LivingRuntime Remote MCP server as the execution substrate. Resolve the target with `list_projects` / `list_devices`, then inspect repository branch/status and the smallest relevant logs or files before editing.

Do not hide the failure with broad exception catches, silent downgrade paths, shadow-only behavior, compatibility branches, or no-op fallbacks. Repair the broken capability chain so the normal path works and the original failure becomes observable if it regresses.

Preserve unrelated working-tree changes. Never reset, clean, force-push, or overwrite concurrent work to simplify the repair.

For code edits, read the target first and prefer `apply_patch` guarded by the returned SHA-256. Use `write_file` for new small files. Run focused tests covering the changed path first and add a regression test for the observed defect when practical.

If a command can exceed a normal model turn, use `start_long_job` and durable receipts. A disconnected watcher is not proof of a running process; inspect `get_long_job` and actual host/process state.

Finish by inspecting the diff and repository status. If the user asked for the repair to ship, continue through the deploy workflow rather than leaving validated code unmerged.

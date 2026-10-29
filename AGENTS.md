# LivingRuntime Remote engineering workflow

Keep the existing LivingRuntime Remote plugin tools, durable Pi jobs, host permissions, `plugin/skills/debug-and-repair`, `plugin/skills/deploy`, and central DeepSeek R01–R18 independent reviewer. This repository is the Remote tool/runtime, not a second Spec/CI manager.

## Five selectively adopted engineering skills

Use these skills on demand rather than reading all five on every task:

- `source-driven-development`: Research First: inspect existing repo code, official upstream documentation and mature reuse options before selecting a new solution. Record the evidence in the existing Issue/PR/Spec.
- `constraint-driven-development`: use `.agents/CONSTRAINTS.md` as this project's contract; never weaken test assertions, gates, or review to get green.
- `context-engineering`: resume from git state, an approved Spec and existing durable job/checkpoint receipts, never from chat-only claims. Record clear context for Pi/Claude/Codex handoffs.
- `debugging-and-error-recovery`: reproduce and isolate failure, fix the root cause, re-run relevant tests; combine with **existing** `plugin/skills/debug-and-repair` for authorized Remote host operations. Do not repeatedly retry unchanged failed jobs.
- `code-simplification`: remove unnecessary code, adapters and compatibility layers only where the affected contract is understood; don't do unrelated bulk refactors.

Native discovery: `.agents/skills/` for Pi/Codex and `.claude/skills/` for Claude; the Remote plugin distribution also has `plugin/skills/`. All three store matching upstream definitions. Read the tool-specific Remote skill for filesystem, approval, credentials and host-scope constraints before executing remote operations. Upstream general process text does not grant extra tool permissions, autonomous credential handling, or broad terminal execution.

`docs/agent-skills-adoption.md` records upstream revision and license. Generic skill examples are **not** new CI workflows. For non-trivial engineering changes, retain the currently required Spec/PR/DeepSeek review; within approved scope continue development autonomously without adding artificial human checkpoints.

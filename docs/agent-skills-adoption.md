# Shared engineering skills for LivingRuntime Remote

Approved by owner on 2026-10-27: adopt exactly the following five:
`constraint-driven-development`, `context-engineering`, `debugging-and-error-recovery`, `source-driven-development`, `code-simplification`.

Source: [jboone1989/agent-skills](https://github.com/jboone1989/agent-skills) (fork of [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills)) at commit `aeac41d4d005999432bb7312f7ff06383a91d59d` / v0.6.13.

Location: `plugin/skills` for Remote plugin packaging; `.agents/skills` for Pi/Codex; `.claude/skills` for Claude Code. All copies use the same exact Git blob contents. Includes `references/floor-guard.md` for the constraint skill; the reference does NOT automatically install a second checker.

Project constraints: `.agents/CONSTRAINTS.md`. Existing reviewer: `docs/claude-review-checklist.md`. Existing targeted Remote debug skill: `plugin/skills/debug-and-repair/SKILL.md`.

Validation checklist: five SKILL.md entries in all three roots with identical Git blob content; constraint reference in all three; no changed Remote executables, permissions or GitHub Actions.

The new plugin skill files only become available to deployed ChatGPT connectors when a compatible Remote plugin package/release installs them; merging source code is not proof of a deployed skill catalog.

License: MIT; Copyright (c) 2025 Addy Osmani; full text in [agent-skills-MIT-LICENSE.md](third-party/agent-skills-MIT-LICENSE.md).

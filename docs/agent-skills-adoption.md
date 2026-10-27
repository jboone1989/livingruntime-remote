# Shared engineering skills — provenance and routes

Owner-approved five skills from [jboone1989/agent-skills](https://github.com/jboone1989/agent-skills) (fork of [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills)) at commit `aeac41d4d005999432bb7312f7ff06383a91d59d`, v0.6.13:

- constraint-driven-development
- context-engineering
- debugging-and-error-recovery
- source-driven-development
- code-simplification

**Single canonical Skill body per name:** `plugin/skills/<name>/SKILL.md`, also used by the Remote plugin package. Discovery adapters at `.agents/skills/<name>/SKILL.md` (Pi/Codex) and `.claude/skills/<name>/SKILL.md` (Claude) must load that full local canonical body before doing work. The constraint Skill's `references/floor-guard.md` sits by its canonical body.

`.agents/CONSTRAINTS.md` is the quality baseline. The existing `plugin/skills/debug-and-repair` still governs authorized host work; the Claude R01–R18 review remains the independent merge gate. New skills do not grant host permissions, create new CI jobs, or insert extra human confirmations for approved work.

Adoption is split across two focused PRs to respect the existing **80KB Claude review diff budget** without enlarging/bypassing that review rule. Installing the files in Git does not prove a deployed Remote plugin is updated.

License: MIT, Copyright (c) 2025 Addy Osmani. Full notice: [agent-skills-MIT-LICENSE.md](third-party/agent-skills-MIT-LICENSE.md).

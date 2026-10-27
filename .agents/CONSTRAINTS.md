# LivingRuntime Remote engineering baseline

The existing Remote safety and review contracts are authoritative. This is a project-specific quality bar for `constraint-driven-development`, not a new quality-gate service.

- Preserve project / device scoping, exact command approvals, credential leases and durable job receipts enforced by LivingRuntime Remote tools. Do not use generic skill commands to bypass them.
- Review the approved Spec / Issue and current branch before implementation. Do not invent new subsystems when an official or existing primitive works.
- No deleted/skipped tests, weakened assertions or review gates to manufacture passing results; distinguish unrun verification from passing verification.
- Repair root causes rather than adding broad catch/fallback compatibility code that masks failures. Confirm changed behavior with focused tests.
- Prefer small cohesive functions; review mixed-responsibility functions >100 lines, without mechanical limits or unrelated refactoring.
- Preserve working-tree changes from other agents. Never force-push, clean or reset another agent's work just to integrate these skills.
- An already-approved task should advance without recurring approval prompts. Escalate actual scope changes, missing user credentials, or host commands requiring explicit approval.
- Use existing CI, GitHub review, and project test commands as implemented. No arbitrary numeric coverage/performance budgets until measured and separately approved.

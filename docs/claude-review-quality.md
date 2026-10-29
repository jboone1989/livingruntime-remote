# DeepSeek Reviewer: adversarial code quality contract

This is the review policy for the existing PR review workflow, implemented in `plugin/scripts/claude_pr_review.py`. DeepSeek is the sole model provider; the workflow and script retain legacy names for compatibility. This is not a new agent service.

DeepSeek must challenge whether each new piece of code is necessary. An author saying "safer" or "tests pass" is not evidence.

| Failure mode to detect | Review question | Required evidence |
| --- | --- | --- |
| Overengineering | Is there a smaller implementation using the existing subsystem? | Compare with actual owners and callers |
| Duplicate code | Were two implementations, queues, APIs, settings or sources of truth left behind? | Point to both paths and their differing behavior |
| Speculative compatibility | Are version bridges, multi-path adapters, retries, feature flags or fallbacks added for clients that do not exist? | Identify the real caller or incident; otherwise request removal |
| Error masking | Does code catch and ignore errors, return defaults or report success without doing the work? | Trace the failure to the reported output |
| Fabricated information | Are placeholder sources, estimated metrics, fake rows or hard-coded success displayed as live results? | Trace each claim to a real data source |
| Test laundering | Did an agent skip tests, change assertions, make mocks unconditional or update snapshots to hide a defect? | Compare expected behavior with the real path tested |
| Weakened CI | Were guards, permissions, lint rules or test thresholds loosened to make a check pass? | Show the removed guarantee and failing scenario |
| Incomplete replacement | Were obsolete code, routes, configuration, flags or tests retained after the new solution was introduced? | Identify the dead or conflicting paths |
| Scope drift | Does the diff deviate from the prior approved Spec or omit an acceptance condition? | Identify the pinned spec and the missing behavior |
| Unverified delivery | Does the PR claim a successful deployment or live test that was never executed? | Distinguish actual receipts from CI mocks |

## Complete review coverage (R01–R18)

The operational [Claude review checklist](claude-review-checklist.md) is the **single source of truth** for the full checklist. The existing reviewer reads it into its quality prompt **on every applicable code, runtime prompt or deployment/config PR**; DeepSeek must examine each applicable item, not only the ten anti-patterns below. This includes FERRO's original module-ownership guidance: most functions under 50 lines, scrutinize >100-line mixed-responsibility functions; cohesive source modules often 50–100 lines when sensible. Length by itself is **never** a reason to fail a PR or invent extra layers.

The anti-pattern table above gives examples, not the complete scope. The model must inspect real callers, types, state transitions, DB writes, concurrency, UI, provider behavior, CI, deployment and test validity as applicable. No extra reviewer process or service was introduced.

## Blocking standard

A concrete, high-confidence, PR-introduced defect is blocking; a speculative cleanup or mere style preference is not. Legitimate test fixtures are allowed **only when clearly labeled as test data** and never used as production evidence. Do not demand an abstraction or defensive layer merely for hypothetical future safety.

For each finding supply **file and changed line, evidence, consequence, and the smallest fix**. A reviewer must not rewrite the code, invent evidence, or turn a failed check green by hiding a failure. Prefer deleting unnecessary code over adding another compatibility path.

## Process

1. Read the prior approved Spec, the actual diff and the existing implementation.
2. Use the independent DeepSeek review pass to challenge the failure modes above; no Claude subscription or native Claude review is used.
3. Independently verify each candidate with a second read-only model pass.
4. Record only reproducible, changed-line findings; `deepseek-review` is the primary status and `claude-review` mirrors it only for legacy branch-rule compatibility.
5. A green CI or mocked test is not proof of deployment or user-observable behavior.

The cross-repo reviewer uses this contract when code changes are present. GitHub's existing rulesets determine which branches require its status.
# Claude Reviewer: adversarial code quality contract

This is the review policy for the **existing** self-hosted `claude-pr-review` workflow, implemented in `plugin/scripts/claude_pr_review.py`. It is not a new agent service.

Claude must challenge whether each new piece of code is necessary. An author saying "safer" or "tests pass" is not evidence.

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

## Blocking standard

A concrete, high-confidence, PR-introduced defect is blocking; a speculative cleanup or mere style preference is not. Legitimate test fixtures are allowed **only when clearly labeled as test data** and never used as production evidence. Do not demand an abstraction or defensive layer merely for hypothetical future safety.

For each finding supply **file and changed line, evidence, consequence, and the smallest fix**. A reviewer must not rewrite the code, invent evidence, or turn a failed check green by hiding a failure. Prefer deleting unnecessary code over adding another compatibility path.

## Process

1. Read the prior approved Spec, the actual diff and the existing implementation.
2. Check correctness with Claude's native review; separately challenge the failure modes above.
3. Independently verify each candidate with a second read-only model pass.
4. Record only reproducible, changed-line findings; `claude-review` fails if any blocking finding remains.
5. A green CI or mocked test is not proof of deployment or user-observable behavior.

The cross-repo reviewer uses this contract when code changes are present. GitHub's existing rulesets determine which branches require its status.
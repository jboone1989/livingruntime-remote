# GitHub PR → Claude Code (no separate Connector)

This uses GitHub's native `pull_request_target` event and a **repository-scoped**
self-hosted Actions runner on VM09. It does not expose a webhook, install a
ChatGPT Claude app, or use an Anthropic API key.

## One-time activation

1. In **jboone1989/livingruntime-remote → Settings → Actions → Runners → New
   self-hosted runner**, follow GitHub's current Linux x64 runner instructions
   as the same `ubuntu` account that owns the working `claude auth status`.
   Register it with the additional label **`claude-review`**.
   Run it as a persistent service using the official `svc.sh` instructions.
2. Check `claude auth status` on that host reports a `claude.ai` subscription.
   The workflow never passes `ANTHROPIC_API_KEY` or any Claude OAuth secret.
   Disable Claude extra usage in account settings if you need a strict no-overage
   subscription budget.
3. Verify GitHub reports the runner **online** and that its labels include
   `self-hosted`, `Linux`, `X64`, `claude-review`.
4. Open or update a **same-repository** non-draft PR. Inspect the
   `Claude PR review (subscription CLI)` check, SHA-specific
   `claude-review` commit status, and one review comment.

The only server dependencies are the existing `/home/ubuntu/.local/bin/claude`
and `gh` binaries and Python 3. No extra Cloudflare endpoint or room service.
GitHub's temporary `GITHUB_TOKEN` is scoped to the review workflow and used
for reading the PR and posting the result; it is not a Claude API credential.

The workflow runs **trusted default-branch code** under `pull_request_target`.
It rejects forks and drafts. Claude Code runs its bundled `/code-review high`
against the exact merge-base-to-PR-commit range using the authenticated local
subscription. It runs in plan mode, with edits, MCP integrations and hooks
disabled. Native review needs Git history and diff access, so unlike the
previous text-only reviewer it may use Git read commands; it must not execute
PR code or tests. Sonnet independently verifies candidate findings before
the script posts a GitHub report.
Reviews are pinned to the PR head SHA; if the head changes mid-review, the
old attempt does not post. Each SHA is reviewed once, with a cap on repeated
blocked rounds. CI/tests and human approval remain separate.

**Scope:** this workflow and its repo-scoped runner cover
`jboone1989/livingruntime-remote` only. Other repositories need their own
approved workflow/runner registration, or a separately designed organization-
wide routing setup. On 2026-10-23, the repository-scoped VM09-ClaudeReview
runner was registered with the `claude-review` label and confirmed online.
The reviewer is considered operational only after an actual PR event yields
the expected commit status and review comment.

## Central review queue (2026-10-29)

`Claude PR review (subscription CLI)` uses one repository-wide native GitHub
Actions concurrency group, `central-claude-pr-review`, with `queue: max` and
`cancel-in-progress: false`. EventGraph review dispatches and this repository's
own PR reviews therefore take turns through the existing self-hosted runner;
EventGraph Check, Docker and E2E still run independently.

GitHub retains up to 100 pending workflow runs in this group. FIFO is based on
the time a workflow begins waiting, not necessarily dispatch time or PR number.
If the queue exceeds 100, additional runs may be cancelled; the queue is **not**
an unbounded durable workboard. Existing per-SHA checks refuse stale heads,
and each completed or failed review workflow frees the next position. Failed
reviews are **not** silently treated as successful; CI and `claude-review`
required status checks remain enforced by GitHub.

This applies to newly triggered workflows after the configuration reaches the
default branch; it does not retroactively regroup already-queued runs.

## Native review benchmark (2026-10-23)

Against the original buggy PR #17 revision (`717da62`), the old Haiku-first
reviewer reported 0 candidates, while native `/code-review high` returned 8;
independent Sonnet verification retained 2 reproducible defects. On the
documentation-only PR #19 control, native review returned a documentation
suggestion that Sonnet rejected. Findings are non-deterministic; a successful
review never replaces CI or human approval.

Original tested review code: `jboone1989/claude-connector/automation/review_worker.py`.
The code was reused here so decommissioning the old Claude Connector does not
remove the review mechanism.

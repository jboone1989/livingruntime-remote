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
It rejects forks and drafts. Review worktrees are read-only to Claude:
`Read,Glob,Grep` only, no repo hooks, no MCP, no Bash, no test execution.
Reviews are pinned to the PR head SHA; if the head changes mid-review, the
old attempt does not post. Each SHA is reviewed once, with a cap on repeated
blocked rounds. Initial pass uses Haiku; Sonnet verifies only when the first
pass reports candidate findings. CI/tests and human approval remain separate.

**Scope:** this workflow and its repo-scoped runner cover
`jboone1989/livingruntime-remote` only. Other repositories need their own
approved workflow/runner registration, or a separately designed organization-
wide routing setup. On 2026-10-23, the repository-scoped VM09-ClaudeReview
runner was registered with the `claude-review` label and confirmed online.
The reviewer is considered operational only after an actual PR event yields
the expected commit status and review comment.

Original tested review code: `jboone1989/claude-connector/automation/review_worker.py`.
The code was reused here so decommissioning the old Claude Connector does not
remove the review mechanism.

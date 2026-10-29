@AGENTS.md

For PR review itself, use `docs/model-review-checklist.md` (R01–R18) and `docs/model-review-quality.md` as the independent review contract. Claude Code CLI remains the read-only harness (Read/Glob/Grep); every model request is routed to `REVIEW_BASE_URL` using `REVIEW_API_KEY`, with optional `REVIEW_MODEL`. No Anthropic account login, OAuth, subscription, or Anthropic-issued credential is required. `claude-review` and `deepseek-review` are compatibility status aliases; `model-review` is canonical. The added skills do not replace, loosen or multiply that reviewer.

# Claude Code + DeepSeek Flash provider profile

Source: [DeepSeek's Claude Code integration guide](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/).

The native `claude-code` adapter keeps Claude subscription as its default. To run a single DeepSeek Flash task, launch the native adapter with `options={"provider_profile":"deepseek-flash"}`. Do not pass `model="opus"` or `fallback_model` in a DeepSeek-profile task. The Flash model is selected by the child-process environment and does not require a second agent.

A DeepSeek run receives only the DeepSeek-compatible endpoint, its isolated auth token, and the Flash model mappings. The subscription run removes those overrides. Neither profile writes credentials to session records or Git.

### VM09 one-time credential setup and smoke

The existing `DEEPSEEK_API_KEY` GitHub Repository Secret is not automatically available to the Remote connector process. The restricted [smoke workflow](../.github/workflows/deepseek-profile-smoke.yml) runs only on main or by explicit dispatch, requires the known VM09 self-hosted review runner identity and writes the secret to `$HOME/.livingruntime/credentials/deepseek-api-key` with owner-only permissions. The key must never be printed or committed.

The smoke calls the same native Claude CLI using the DeepSeek profile and checks an exact text response. It does not replace a live Remote plugin deployment. To make the profile available through the ChatGPT connector, release and deploy the updated Remote connector, then invoke `start_agent` with the profile and verify its normal durable result.

### Limitations

This PR adds a manual per-task provider profile and a real endpoint smoke. **It does not yet implement automatic retry after a Claude subscription quota error.** A quota fallback needs an explicit classification and a fresh task session so that work with side effects is not replayed. A `STALLED` worker is not a quota failure. The `novel-skills` source is independent of model routing and must be read by each actual reviewer.

Tests: `pytest -q plugin/tests/test_deepseek_profile.py plugin/tests/test_claude_adapter.py`.

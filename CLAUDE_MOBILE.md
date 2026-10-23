# Claude Code through LivingRuntime Remote (including ChatGPT iOS)

No separate ChatGPT Custom MCP App registration is required for this route. The
already-connected LivingRuntime Remote App invokes the existing Claude adapter
using its supported `exec` tool. Parley backend is not launched.

## Commands

Execute these with `project="livingruntime-remote"`, `timeout_seconds=25`:

```json
{"argv":["python3","plugin/scripts/claude_mobile.py","ask","--message","请只回复 CONNECTOR_OK","--wait","18"]}
```

To continue the same conversation, use the returned `session_id`:

```json
{"argv":["python3","plugin/scripts/claude_mobile.py","send","--session","<returned_session_id>","--message","请继续审查","--wait","18"]}
```

Other actions: `start --message "..." ` (detached), `read --session <id>`,
`sessions`, `stop --session <id>`. The response is JSON and includes the
actual model output or terminal/error status.

## Guarantees and limits

- The only agent started is the explicitly allowlisted `claude-code` adapter.
- Native Claude login is used on the VM09 host; no Claude API key is sent to ChatGPT.
- An existing session can be resumed across ChatGPT turns.
- Claude is run with `--permission-mode plan --max-turns 10` by the installed adapter,
  so this path is for discussion/review, not for editing a repository.
- Default project `livingruntime-remote` is preapproved in the VM09 agent policy.
  Additional projects require explicit host policy changes.
- This is a **Claude via LivingRuntime Remote** path. It does not register a
  separately named mobile ChatGPT App called Claude Connector.
- Follow normal Remote exec approval rules. An approval_required response is a
  real boundary, not a reason to bypass it.

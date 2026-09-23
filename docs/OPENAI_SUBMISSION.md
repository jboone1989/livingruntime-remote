# OpenAI Plugin Submission Runbook

LivingRuntime Remote is prepared as a remote-MCP-only plugin submission.

## Fixed public values

- Plugin name: `LivingRuntime Remote`
- Category: `DEVELOPER_TOOLS`
- MCP server URL: `https://remote.livingruntime.com/mcp`
- OAuth / OIDC issuer: `https://remote.livingruntime.com`
- Domain challenge base URL: `https://remote.livingruntime.com`
- Website: `https://remote.livingruntime.com/`
- Support: `https://remote.livingruntime.com/support`
- Privacy: `https://remote.livingruntime.com/privacy`
- Terms: `https://remote.livingruntime.com/terms`
- Install: `https://remote.livingruntime.com/install`
- Demo recording: `https://remote.livingruntime.com/demo.mp4`
- Demo page: `https://remote.livingruntime.com/demo`
- Source: `https://github.com/jboone1989/livingruntime-remote`
- Submission import file: `plugin/chatgpt-app-submission.json`

## Authentication

Authentication type is OAuth/OIDC with PKCE.

Discovery endpoints:

- `/.well-known/oauth-authorization-server`
- `/.well-known/openid-configuration`
- `/.well-known/oauth-protected-resource/mcp`

Supported review scopes include:

- `remote:read`
- `remote:write`
- `openid`
- `email`
- `offline_access`

## Reviewer credentials

Username:

`openai-review@livingruntime.com`

The current reviewer password is stored only on the production relay in the root-only operational credential file. Do not commit it to this repository.

The reviewer account:

- requires no registration;
- requires no email confirmation;
- requires no SMS confirmation;
- requires no MFA;
- has a persistent paired Connector;
- sees only the isolated `demo` project;
- cannot see the operator's production repositories;
- has no allowlisted production systemd units.

## Review test contract

The import JSON contains exactly five positive test cases and three negative test cases.

Positive tools:

1. `connection_status`
2. `list_projects`
3. `read_file`
4. `git`
5. `write_file`

All sixteen public MCP tools have explicit `readOnlyHint`, `openWorldHint`, and `destructiveHint` annotations. FastMCP also generates an output schema for every public tool.

## Domain verification

The relay already serves:

`https://remote.livingruntime.com/.well-known/openai-apps-challenge`

When the OpenAI submission portal generates the domain-verification token, replace the current placeholder with that exact token, restart the relay, and verify that the endpoint returns only the token.

Do not submit while the placeholder value is still active.

## Portal prerequisites

Before creating or submitting the draft:

1. Use the OpenAI organization that will own the published plugin.
2. Complete individual or business verification for the public publisher identity.
3. Ensure the submitter is an organization owner or has Apps Management write permission (`api.apps.write`).
4. Create a plugin submission with the **Includes MCP** option.
5. Supply the public MCP URL directly; do not reference an existing custom connector.
6. Import or copy the values from `plugin/chatgpt-app-submission.json`.
7. Supply the reviewer username and root-only reviewer password.
8. Complete the domain challenge.
9. Select the intended country availability.
10. Review every tool annotation explanation and submit for review.

## Release notes text

LivingRuntime Remote 0.4.17 provides bounded remote-development tools for user-controlled machines, with OAuth/OIDC authentication, refreshable ChatGPT sessions, one-time Connector pairing, configured-root and service allowlists, auditable write actions, cross-platform Connector releases for Windows, Linux, and macOS, and Pi Remote completion-driven ChatGPT continuation.

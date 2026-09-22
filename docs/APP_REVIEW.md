# ChatGPT Plugin Directory review environment

LivingRuntime Remote keeps a dedicated review account and Connector so directory review never needs access to production development projects.

## Public endpoints

- MCP server: `https://remote.livingruntime.com/mcp`
- OAuth / OIDC issuer: `https://remote.livingruntime.com`
- Demo recording: `https://remote.livingruntime.com/demo.mp4`
- Demo page: `https://remote.livingruntime.com/demo`
- Support: `https://remote.livingruntime.com/support`
- Privacy: `https://remote.livingruntime.com/privacy`
- Terms: `https://remote.livingruntime.com/terms`
- Install: `https://remote.livingruntime.com/install`

## Reviewer account

The reviewer username is `openai-review@livingruntime.com`.

The password is intentionally **not** stored in this repository. The production host keeps the current review credential in a root-only operational file and it should be copied into the OpenAI submission form only when submitting or updating the plugin.

The reviewer account:

- requires no registration;
- requires no email confirmation;
- requires no SMS confirmation;
- requires no MFA;
- has a persistent paired Connector;
- sees only the isolated `demo` project;
- cannot see the operator's production repositories;
- has no allowlisted production systemd units.

## Isolation boundary

The review Connector uses a separate Remote config from the operator Connector.

It exposes only:

- project alias: `demo`
- one isolated Git repository created for review
- no production project aliases
- no allowlisted systemd units

The review Connector runs as a user systemd service and has its own relay device credential. Its raw device token remains on the Connector machine.

## Positive review cases

The submission import file intentionally contains exactly five positive cases:

1. `connection_status`
2. `list_projects`
3. `read_file` in project `demo`
4. `git` status in project `demo`
5. repeatable `write_file` to `review-note.txt`

The JSON also contains exactly three negative cases.

## Validation

Before submission or resubmission:

1. Confirm `https://remote.livingruntime.com/healthz` reports the current release.
2. Confirm the review Connector is online.
3. Run the five positive cases against the review device.
4. Confirm `connection_status.configured_roots` contains only the review workspace.
5. Confirm `list_projects` returns only `demo`.
6. Confirm all public MCP tools have explicit read-only, open-world, and destructive annotations.
7. Confirm all public MCP tools expose title, description, input schema, and output schema.
8. Confirm the submission file contains exactly five positive and three negative cases.
9. Confirm `/demo.mp4` is publicly reachable over HTTPS.
10. Confirm the domain challenge endpoint returns the exact portal token before final submission.

## After review

If the reviewer account is no longer needed, revoke its paired device and rotate or remove the review OAuth account. Do not reuse the reviewer credentials for operator access.

# Privacy policy

LivingRuntime Remote connects ChatGPT-compatible clients to machines and repositories that the user is authorized to control.

## Data handled

LivingRuntime Remote may process:

- Account identity needed for OAuth sign-in, including email address and verification state.
- OAuth client metadata and hashed access/refresh-token values with their scopes and expiration times. Raw bearer and refresh tokens are not stored.
- Pairing and device metadata such as device ID, connector name, creation time, and last-seen time. Device bearer tokens are stored only as hashes by the relay.
- Tool inputs and outputs required to perform bounded file, Git, process, service, and log operations.
- Non-secret connection metadata configured on the Connector machine, such as an SSH host alias, allowed workspace roots, project aliases, and allowlisted systemd unit names.
- Local audit records written on the user's Connector or target machine.

Successful relay task results are consumed and deleted after delivery to the requesting client. Stale queued, claimed, or completed relay tasks are eligible for cleanup after 24 hours.

## Credentials

LivingRuntime Remote does not store SSH passwords or SSH private keys on the public relay. SSH credentials remain in the user's existing SSH configuration on the Connector machine.

OAuth account passwords are stored only as salted scrypt hashes. Raw OAuth bearer, refresh, and device tokens are not stored in relay databases.

## Sharing

When LivingRuntime Remote is used with ChatGPT or another compatible client, tool arguments and results travel through that client's normal MCP request path and through the LivingRuntime Remote relay so the paired Connector can execute the requested bounded operation.

Do not place secrets in tool arguments or file contents that you do not want transmitted through the connected client and relay.

## Retention and control

OAuth account records and paired-device records remain until the account or device is revoked or removed. Expired OAuth authorization state and token hashes are cleaned up by the service. Local configuration and audit logs remain on the user's machines until deleted.

ChatGPT-side retention follows the user's OpenAI account or workspace settings.

## Contact

https://remote.livingruntime.com/support

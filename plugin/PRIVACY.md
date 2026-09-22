# Privacy policy

LivingRuntime Remote connects ChatGPT and other compatible MCP clients to machines that the user controls through a paired Connector.

## Data the service handles

- Account identity data required for OAuth sign-in, including email address and verification state.
- Non-secret Connector metadata such as the Connector name, online state, configured workspace roots, project aliases, and allowlisted service names.
- Tool inputs and outputs for bounded file, Git, command, process, service, patch, and log operations requested by the user.
- Pairing and session records required to route requests to the user's paired Connector.
- Local audit records written on the Connector machine for Remote operations.

## Credentials and secrets

LivingRuntime Remote does not store SSH passwords or SSH private keys. Those remain in the user's existing SSH configuration on the Connector machine.

OAuth access and refresh tokens are stored by the LivingRuntime Remote authorization service only as required to maintain authenticated sessions. Connector device credentials are stored locally on the Connector; the relay stores only the information required to authenticate and route the paired device.

Do not put secrets in tool arguments or file contents unless you intend those values to be sent through the connected client as part of the requested operation.

## Sharing

When LivingRuntime Remote is used with ChatGPT, tool arguments and results travel through the normal OpenAI app/MCP request path. The Connector then performs the requested bounded operation on the user's machine or SSH-reachable host.

LivingRuntime Remote does not sell user data.

## Retention

OAuth, pairing, and relay state is retained only as needed to provide active Remote sessions and device pairing. Users can revoke a paired Connector with `disconnect_device`. Local configuration and audit logs remain on the user's machines until they delete them. ChatGPT-side retention follows the user's OpenAI account or workspace settings.

## Contact

https://remote.livingruntime.com/support

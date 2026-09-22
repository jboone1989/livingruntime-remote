# Privacy policy

LivingRuntime Remote is a private development plugin/app for operating a host you already control.

## Data the plugin handles

- Non-secret connection metadata that you configure locally, such as an SSH host alias, workspace roots, systemd unit names, and an optional OpenAI `tunnel_` identifier.
- Tool inputs and outputs for file, Git, process, service, and log operations on that host.
- Local audit records written on the machine that runs the MCP server, typically under `~/.livingruntime/remote-audit.jsonl` or the gateway audit log.

## Data the plugin does not handle

- SSH passwords
- SSH private keys
- OpenAI runtime API keys, which must stay in environment variables such as `CONTROL_PLANE_API_KEY`
- Payment data, government identifiers, or biometric data

## Sharing

When you connect the MCP server to ChatGPT through Secure MCP Tunnel or a public HTTPS endpoint, tool arguments and results are sent to OpenAI as part of that product’s normal app/MCP request path. The private MCP server and SSH endpoint stay inside your network. Do not put secrets in tool arguments or file contents that you do not want included in that path.

## Retention

Local configuration and audit logs remain on your machines until you delete them. ChatGPT retention follows your OpenAI account and workspace settings.

## Contact

https://remote.livingruntime.com/support

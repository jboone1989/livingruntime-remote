# LivingRuntime Remote MCP Gateway

A deliberately small remote execution gateway for LivingRuntime development hosts.

It exists as a backup execution path when a primary remote-control plugin is unavailable or rate-limited. The gateway is intentionally narrower than a general remote shell: file paths are confined to configured roots, service control is exact-unit-allowlisted, output is bounded, bearer authentication is mandatory for MCP requests, and every tool call is appended to an audit log.

## Tools

- `connection_status` — preferred connectivity/authorization probe; never returns secrets
- `gateway_status` — compatibility alias; prefer `connection_status`
- `exec` — argv-only execution; `bash -c` / `bash -lc` are blocked
- `read_file`
- `write_file` — optional SHA-256 compare-and-swap
- `list_dir`
- `git`
- `process` — list, or terminate only same-user processes whose cwd is inside an allowed root
- `systemd` — exact allowlist only
- `logs` — exact allowlist only

## Security boundary

This is a scoped remote development agent, not a hostile-code sandbox.

The gateway deliberately runs as an unprivileged Unix user and constrains filesystem paths and systemd units. However, development interpreters such as Python are allowed, so a caller that is already authorized can run code with that Unix user's permissions. The bearer token therefore needs to be treated like an SSH credential.

The service binds to `127.0.0.1` by default. Do not expose port 8765 directly to the public internet.

## Install

From this directory on the target host:

```bash
sudo bash ./install.sh \
  --user ubuntu \
  --allowed-root /home/ubuntu \
  --unit content-agent.service
```

Add additional `--unit ...` flags only for services the gateway truly needs to control. The installer creates exact sudoers entries for only `start`, `stop`, and `restart` of those named units.

After install:

```bash
systemctl status remote-mcp-gateway.service
curl http://127.0.0.1:8765/healthz
sudo grep MCP_GATEWAY_TOKEN /etc/remote-mcp-gateway.env
tail -f /home/ubuntu/.local/state/remote-mcp-gateway/audit.jsonl
```

The MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

For a remote MCP client, put it behind a trusted MCP tunnel or a TLS edge instead of opening the port directly.

## Tests

The policy layer uses only the Python standard library:

```bash
cd gateway
python3 -m unittest -v test_policy.py
```

The test suite covers allowed-root enforcement, symlink escape prevention, command policy, git credential override rejection, exact systemd unit allowlisting, and MCP `tools/list` discovery for `connection_status`, `read_file`, `git`, and `logs`.

```bash
cd gateway
python3 -m unittest -v
```

## ChatGPT product constraint

As of September 2026, OpenAI documents full write/modify custom MCP apps for ChatGPT Business and Enterprise/Edu. Pro supports custom MCP read/fetch in developer mode; Plus is not documented as supporting a private write-capable custom MCP app.

So this server is useful infrastructure now and can be used by compatible MCP clients, but on a Plus ChatGPT account it is not yet a drop-in private replacement for a write-capable published plugin such as Remote Desktop Commander. Direct ChatGPT write access requires a supported workspace plan or a separately published/approved plugin path.

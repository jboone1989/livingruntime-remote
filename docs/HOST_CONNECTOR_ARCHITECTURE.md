# Host-local Connector architecture

## Desired topology

Each physical or virtual host runs one unprivileged Connector process. Each Connector
opens outbound HTTPS to the public Relay, which owns authentication, routing, and
durable receipts. SSH is a legacy compatibility bridge, not a default enrollment
path. Host IDs must remain stable across Connector upgrades and reinstallations.

Vultr currently runs Relay and two Connectors (one privileged maintenance route
and one restricted local route). Only the restricted local Connector should remain
as the ordinary host route. Relay deployment rights should instead be granted via
a narrowly-scoped admin-controlled deployment service or explicit limited sudo
policy. Do not combine generic root access with routine task execution.

## Migration, without losing recovery access

1. Leave legacy SSH routes operational while introducing local Connectors on each
   host; avoid overwriting an existing Connector's local configuration.
2. Pair a new Connector, verify host identity, its allowed workspaces, autostart,
   read-only tools, and job receipts through that exact Connector identifier.
3. Rebind project routing to the newly registered host, preserving logical names.
4. Withdraw obsolete SSH routes only after a second verification from the Relay.
5. Retire Vultr's legacy privileged Connector only after independent controlled
   Relay deployments and recovery are proven.

Do not deduplicate hosts on display names, IP addresses, or reported hostnames:
none is a durable authenticated identity. Bind an owner-scoped stable host ID
at enrollment, and prevent duplicate active registrations before changing the
dashboard to claim a global unique host count.

Current control-plane UI labels the default Connector's managed hosts explicitly
and separately displays Relay-registered Connectors. It must not claim this is
a fully migrated 1:1 host inventory. 

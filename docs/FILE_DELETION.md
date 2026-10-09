# Bounded filesystem deletion (0.4.57)

The dedicated MCP tool is `delete_path`. It accepts one literal `path` plus optional `project` / `device`, `recursive`, `dry_run` and `expected_sha256`.

- Regular files are deleted directly. Directories require `recursive=true`. A leaf symlink is unlinked; its target is never traversed.
- `dry_run=true` returns planned counts without changing the filesystem.
- `expected_sha256` accepts a 64-character SHA-256 for regular files. A stale digest, a non-file target or a missing guarded file fails without deletion.
- An unguarded missing path returns `NOT_FOUND`.
- `DELETED` confirms the selected entry was removed. `PARTIAL_FAILURE` reports actual removed entry/content-byte counts and the failure; recursive deletion is not transactional and should not be blindly retried.
- Limits are 10,000 entries, 512 MiB of logical file contents, and 64 descendant levels. Counts do not promise reclaimed disk space.
- Configured workspace roots, configured project roots and their ancestors are protected. The current user's `.ssh`, `.livingruntime`, `.codex`, `.gnupg` and `.config` are protected recursively.
- POSIX no-follow directory handles keep traversal inside the selected configured root. Parent symlinks, `..` segments, special files and other filesystem devices are rejected. Concurrent tree changes are detected where possible; operations are not atomic.
- Native Windows deletion is unsupported and fails closed. Existing Windows read/write tools continue to work.
- Success, dry-run, partial failure and rejection are audited. The public relay requires the existing `remote:write` OAuth scope and marks the tool destructive.

This API does not grant shells or destructive filesystem commands through dynamic exec permissions. Their hard-deny rules remain in place.

## Activate the new tool

Deploy the same 0.4.57 source to the public relay and each Connector, using the established operator deployment mechanism. Preserve existing configuration, OAuth/device bindings and credential state. Restart the corresponding services through the service manager or an explicitly allowlisted control interface.

Verify all of the following:

1. The deployed public relay health endpoint reports 0.4.57.
2. Connector `capabilities.server_tools` and public `capabilities.public_server_tools` both include `delete_path`.
3. The client has refreshed its action list and exposes the new tool.
4. A disposable file can be previewed, deleted and independently confirmed absent.

A source commit, copied files or a passing test suite does not prove that a running service or the client's cached tool list has upgraded. A service that is not allowlisted must be restarted by its operator; do not work around service-control restrictions.

## Validation

```text
# Connector/POSIX and SSH worker behavior:
python plugin/tests/test_delete_path.py
python plugin/tests/test_mcp_protocol.py
python plugin/tests/test_discovery.py
python plugin/tests/test_packaging.py
python plugin/tests/test_localexec.py
python plugin/tests/test_multidevice.py
python plugin/tests/test_connector.py

# Public relay (with relay/requirements.txt installed):
cd relay
python -m unittest test_server
```

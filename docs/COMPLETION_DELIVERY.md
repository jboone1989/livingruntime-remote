# Long-job completion and recovery

A terminal process receipt, host acceptance of a continuation message, and a
model's acknowledgement of the inspected result are separate facts. None of
these states controls ChatGPT's internal thinking indicator.

## Connector result delivery

The Connector writes results to a SQLite outbox next to its pairing config
(`relay.outbox.sqlite3`). A dedicated uploader retries pending receipts with
bounded exponential backoff. Commands are never re-executed by that uploader.
The queue is scoped to the paired device and relay; re-pairing cannot upload an
old device's results under a new identity. Server-side result hashes make upload
acknowledgements idempotent even after a result has been consumed.

New Connectors request the two-phase claim protocol: Relay offers a task without
authorizing execution; Connector persists a prepared record with a unique token,
then acknowledges that preparation. Relay atomically accepts one token.
An offer lost before local persistence can be offered again. A lost acceptance
response is retried using the same durable token. Execution starts only after
acceptance and a local transition to executing. Old Connectors retain the legacy
claim behavior until upgraded; new Connectors require the new Relay.

A claimed call that outlasts the Relay wait returns DELIVERY_UNCERTAIN and
relay_task_id. Use the read-only get_relay_task tool to recover the original
result without enqueueing another operation. Normal responses and device errors
also carry the receipt ID. Completed results remain queryable for at least
24 hours after completion; unresolved claimed operations are not age-deleted.
A missing receipt never authorizes a destructive retry.

If the Connector restarts between execution and result persistence, the result
is explicitly unknown. Inspect the durable execution receipt and current state
before retrying a command. Delivered outbox rows retain only task identities;
their result payloads are cleared. Do not delete the outbox during an upgrade.

## Completion notifications

The Relay persists one completion event per user/device/job. Watchers claim a
90-second delivery lease so duplicate cards do not automatically send duplicate
notifications. States are `pending`, `sending`, `uncertain`, `accepted` and
`observed`. The model calls `acknowledge_job_completion` after inspecting the
durable result; this does not mark the user's overall goal complete.

The widget remembers host acceptance separately from process completion. If
the Relay acknowledgement fails after host acceptance, recovery only repeats
the acknowledgement, not the message. Definite send failures can be retried.
A message timeout or an expired sending lease is **uncertain**, not failure:
the host may have accepted the message before its acknowledgement was lost.
Automatic resend is suppressed; the card offers a clearly labeled explicit
retry after checking the conversation. Exactly-once ChatGPT message delivery
cannot be guaranteed without a host-supported idempotency protocol.

All widget RPC calls have deadlines. Cosmetic context updates do not block
receipt checks. A detached page cancels outstanding RPCs; restoring a cached
page reconnects the watcher. Unobserved job liveness is reported as `UNKNOWN`,
including the last observation time and refresh error, rather than reusing old
live-process flags.

Closing a page stops its watcher. Durable receipts and delivery records survive,
but this plugin does not invent a background ChatGPT wakeup facility: reopening
the card or attaching a watcher resumes delivery. Host acceptance also does not
prove that the model has finished its next turn.

## Upgrade and validation

Deploy Relay first and then the Connector, preserving the existing Relay
SQLite database and Connector pairing/outbox files. SQLite tables are added
automatically. New watcher URIs invalidate cached HTML; the previous URIs remain
registered. Refresh the connected app's tool snapshot so the app-only claim and
settlement tools and model acknowledgement tool are available. Old cached code
already executing in an open page requires reopening the task card.

The relay tests now require Node.js to execute the actual inline watcher scripts
under message loss, negative acknowledgements, reload, and teardown. Run:

```
python -m unittest discover -s plugin/tests -v
cd relay
python -m unittest discover -v
```

Use the separate dependency sets in `plugin/requirements.txt` and
`relay/requirements.txt`. Full plugin tests require Linux/POSIX (existing
locking and permissions tests); Connector delivery tests also run on Windows.

Host message semantics: [OpenAI UI documentation](https://developers.openai.com/plugins/build/chatgpt-ui).

# Execution contract v1 (local runtime foundation)

This increment adds durable execution facts to the existing local runtime. It does
not introduce a network listener, server deployment, Peer Mesh, automatic task
continuation, or a new canonical conversation database. Existing configuration,
the 300-second reminder policy, local RPC transport, and pending-chat recovery
remain in place.

## Identities and admission

- `root_id`: stable identity of the execution store in one workspace.
- `epoch`: one journal-owning runtime lifetime; changes after restart.
- `session_key`: existing canonical session key, unchanged.
- `source`: originating channel (or `delivery` for an independent push).
- `request_key`: caller's explicit `execution_request_id`, or a generated UUID.
- `turn_id`: logical accepted work; `run_id`: its physical execution attempt.
  V1 has one attempt per accepted turn. A future continuation must explicitly
  create/link another attempt, not reuse a completed attempt.

RPC `id` remains a correlation ID, NOT an idempotency key. Existing callers can
reuse RPC IDs as before. New clients can opt into durable deduplication by passing
`execution_request_id` to `chat.send`, or in `process_direct` metadata. The key is
scoped to `(source, session_key)`; reuse with different canonical input is rejected.
Input comparison includes content, media references, sender, destination and
execution metadata, but excludes RPC correlation and execution request IDs.

Admission happens under the existing session execution gate. The request body,
identities, `accepted` status and `request.accepted` event commit in one SQLite
transaction BEFORE the runtime begins processing. The database also prohibits
two accepted/running executions for the same session. Work still waiting outside
this gate has not been durably accepted. Successful acceptance does not mean the
model ran, a tool succeeded, or a remote recipient received a message.

## States and events

`accepted -> running -> completed | failed | interrupted | needs_review`

An accepted request can also end before execution starts. All terminal transitions
commit together with their event. Completed duplicate requests return the saved
result without invoking the model, tools, or outbound dispatch again. Other
duplicates return their existing status and require explicit handling; no generic
retry or automatic continuation is introduced.

The append-only event kinds are:

| Event | Durable fact |
| --- | --- |
| `request.accepted` | Original input, media references and execution identity |
| `run.started` | The accepted attempt crossed the execution boundary |
| `reply.delta` | Received streamed text/thinking before downstream UI delivery |
| `tool.dispatched` | Tool name, final arguments/context before actual invocation |
| `tool.returned` | Returned structured result, or an exception with unknown effects |
| `messages.committed` | Message rows appended to the existing conversation store |
| `delivery.dispatched` | One MessagePushTool sender part is about to be invoked |
| `delivery.returned` | Sender result, receipt ID when supplied, and certainty |
| `run.error` | Sanitized error class; not the provider's response body |
| `run.ended` | Terminal execution classification and optional result |
| `run.recovered` | Abandoned attempt classified after epoch liveness verification |

Events have a store-wide increasing `seq`, contract version, turn/run/epoch identity
and UTC timestamp. SQLite triggers reject event updates/deletes. Status/result rows
are an index maintained in the same transaction as lifecycle events. The store is
`workspace/private_runtime/execution/events.sqlite3`, uses WAL and FULL synchronous
commits, and is separate from the existing `sessions.db` conversation projection.
`messages.committed` follows the session database commit: a crash between databases
can leave a committed message without that event. Recovery is conservative and
does NOT claim an atomic transaction across the two databases or replay the turn.

## Failure and recovery

Each epoch holds an OS file lock. Recovery acquires an old epoch's lock before
classifying its active attempts; a live runtime is never declared dead merely
because another connection was opened. PID files are not used as liveness proof.
This is not yet the future whole-workspace single-Host lease.

- Accepted/running attempt, no unresolved effect: `interrupted`.
- Dispatch without a confirmed result, including a tool exception or ambiguous
  send failure: `needs_review`.
- Completed attempt: unchanged and replayable as a cached response.
- Recovery itself only appends facts/updates the status index. It does not call a
  model, run a tool, resend a reminder, or fabricate missing external receipts.
- Repeating startup recovery does not append another recovery event.
- Failure to write a tool dispatch prevents invoking the tool. Failure to write
  its result prevents further effects in that execution and leaves the committed
  prefix for recovery. Durability errors are not converted into model-visible
  ordinary tool errors followed by more execution.

Original input and received partial output survive a hard process exit and are
available through the execution queries. V1 does not automatically insert those
events into conversation history or resume model execution. The existing canceled
chat journal still restores canceled messages into conversation history; its tests
remain required. No guarantee is made for bytes not yet received/committed, lost
attachment files, disk corruption, or a filesystem that cannot persist writes.

## Coverage and deliberate boundaries

Ordinary stateful AgentLoop turns (desktop, Telegram and other channels using the
same path) share admission. Stateless/internal work does not claim that identity.
ToolRegistry captures physical invocation boundaries inside a recorded turn;
hook denial occurs before actual dispatch. MessagePushTool records sender parts
for tool, scheduled and proactive sends, creating an independent delivery attempt
when no ordinary turn is active. A sender returning normally confirms its callback
contract, not human reading; absence of a platform message ID is preserved.

The complete proactive reasoning pipeline, separately scheduled subagents,
Telegram live-preview edits, and all bus-based passive transport receipts are not
yet a unified execution lifecycle. They must be brought into the future Host
admission/outbox design. This increment must not be described as exactly-once
Telegram delivery, a durable inbound queue, or full event-sourced model context.
The later [durable transport increment](durable-transport-v1.md) now adds a
Telegram inbox and a shared outbox, including part receipts and recovery. It does
not change the execution contract's conservative rule for interrupted tools.
Conversation deletion and execution audit retention are separate concerns; this
append-only audit currently remains until the workspace is explicitly removed.

## Local query protocol

- `health`: also returns `execution_contract_version`, `root_id`, `epoch`.
- `executions.list`: `session_key`, optional `before` cursor and `limit` (1–100).
- `execution.get`: `turn_id`, or `session_key`, `source`, `execution_request_id`.
- `execution.events`: `turn_id`, `after` sequence, `limit` (1–500), returns
  `events` and `next_cursor`. Use the returned cursor, not `seq + 1`; other turns
  may occupy intervening store-wide sequence numbers.

These are existing local IPC operations, not authenticated remote endpoints.
Snapshot/event subscription handshakes, client credentials and remote permissions
belong to the next Host phase.

## Verification

`tests/backend/test_execution_journal.py` covers atomic admission, immutable events,
duplicate/conflicting requests, concurrent requests, live/dead epochs, strict
dispatch/result write boundaries and repeated recovery. It launches actual child
processes from `tests/e2e/execution_crash_backend.py` and
`tests/e2e/execution_local_crash.py`; fault points call `os._exit(73)` with no finally
or graceful flush. Cases cover accepted input, partial output, before/after a tool
effect, confirmed tool result, uncertain send, and completed result.

`tests/backend/test_session_desktop_services.py` additionally verifies the real
local chat path, distinct RPC/execution identities, saved results, provider errors
and standalone push receipts. All probes use isolated temporary workspaces and
fake providers/transports; they do not send real Telegram messages or modify user
configuration.

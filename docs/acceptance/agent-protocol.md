# Acceptance: agent event protocol and session persistence

Scope: the contract between the agent runtime and the browser, the append-only event log that backs it, and the
HTTP routes that serve it. The agent loop, tools and sandbox are separate capabilities; this one is verified with
recorded events, not with a model. Decisions are in [ADR 0008](../adr/0008-agent-runtime-and-execution-policy.md).

## What exists

| Capability                                                                                              | Where                                                                       |
| ------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Event envelope, 30 payload types, provenance union (source range, byte range, report fact, tool result) | `backend/app/contracts/agent_events.py`                                     |
| JSON Schema, generated and checked against the file                                                     | `docs/contracts/AgentEventV1.schema.json`, `scripts/export_agent_schema.py` |
| Session and command contracts, compacted state                                                          | `backend/app/contracts/agent_session.py`                                    |
| State fold, idempotent over duplicates and gaps                                                         | `backend/app/services/agent/state.py`                                       |
| Gap-free, encrypted, append-only event log; replay and live tail; notification fan-out                  | `backend/app/services/agent/event_log.py`                                   |
| Session create (idempotent), read, state, event pages                                                   | `backend/app/services/agent/sessions.py`, `backend/app/api/agent_routes.py` |
| Tables, guards, grants (revision `0050_agent_sessions`)                                                 | `backend/alembic/versions/0050_agent_sessions.py`                           |

Routes (all under `/api/v1/agent-sessions`): `POST /` (CSRF and `Idempotency-Key` required), `GET /{id}`,
`GET /{id}/state`, `GET /{id}/events?after&limit`, `GET /{id}/stream` (SSE; `Last-Event-ID` or `?after=`).
The message, approval, patch, interrupt, mode, restore and file commands are defined as contracts and are built
with the agent loop.

## How the guarantees are enforced

- **Gap-free sequence.** A writer locks the session row, increments `last_seq`, and inserts the event with that
  sequence in one transaction. The database rejects an event whose sequence differs from `last_seq`, an update
  that moves `last_seq` by more than one, and (deferred to commit) any transaction that leaves a gap.
- **Append-only.** Events and checkpoints cannot be updated or deleted; sessions cannot be deleted; a finished
  session cannot change status; an approval is decided once. The API and worker roles have no UPDATE or DELETE on
  events, and the API role cannot read checkpoint state.
- **Encrypted at rest.** Payloads are AES-256-GCM with the organisation, session, sequence and type as associated
  data, so a row moved elsewhere fails to decrypt. The key is injected; nothing reads it from the environment.
- **Tenant and principal scoping.** Every query is by organisation; a session is readable only by the principal who
  started it and 404s for anyone else.
- **Resume.** The stream replays rows after the client's last sequence, then tails. Replay is capped at 2,000 events
  per connection; beyond that the server sends `stream.reset` (no `id`, so `Last-Event-ID` is kept) and the client
  loads `/state`. A heartbeat comment keeps proxies open.

## Deviations from the protocol text

- Backpressure is pull-based: the database is the queue and the writer is never blocked. A client that falls more
  than 2,000 events behind gets `stream.reset` with reason `backpressure`; there is no separate 256-event buffer.
- Notifications come from a database trigger on insert, so every writer notifies without remembering to. A tail that
  misses a notification still catches up at its poll interval (default 2 s), so a notification only improves latency.
- Client disconnects can arrive while a read is in flight; the stream finishes that short read before stopping so the
  pooled connection is returned cleanly (a cancelled query otherwise leaves a broken connection in the pool).

## Verification

```
cd backend
uv run --frozen pytest -c pyproject.toml tests/agent -q -o addopts=            # contracts, state: no database
# with a disposable PostgreSQL (scripts/phase2_db.py prepare) and CODESPY_TEST_SCHEMA_TARGET=head:
uv run --frozen pytest -c pyproject.toml tests/agent -q -o addopts= -p no:logging
```

46 tests cover: every event type validates and matches the exported schema; the schema file is current; 1,000
concurrent writers produce sequences 1 to 1,001 with no gap; the stream resumed after a disconnect at every
sequence equals the uninterrupted stream; two independent listeners (separate database connections, as two API
processes would have) deliver identical streams with a 60 s poll, so only the notification can wake them; two real
HTTP servers stream the same events over SSE and a client resumes with `Last-Event-ID` on the other; database
guards reject gaps, jumps, edits, deletes and cross-role writes.

## Not done here

The `PgNotifyWaiter` is constructed and started by the composition root (it needs the process lifecycle). Until that
exists a server falls back to polling. Event retention and deletion, the audit log, and the agent loop are separate
capabilities.

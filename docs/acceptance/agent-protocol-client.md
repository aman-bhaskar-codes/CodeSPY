# Acceptance: agent protocol on the client

Scope: the frontend's view of the agent event protocol: generated TypeScript types, a small event parser, and three
recorded sessions that both the backend and the frontend replay. The architecture decision for the editor and
workbench is in [ADR 0010](../adr/0010-workbench-and-editor-architecture.md). No agent UI exists yet.

## What exists

| Part                                                                                                               | Where                                                                                            |
| ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Types generated from the backend JSON Schema                                                                       | `frontend/src/features/agent/protocol/types.gen.ts`, `frontend/scripts/generate-agent-types.mjs` |
| Envelope parser: refuses an unknown protocol version, passes unknown event types through, checks the sequence rule | `frontend/src/features/agent/protocol/index.ts`                                                  |
| Three recorded sessions: normal run, rejected run, run interrupted by a lost worker                                | `frontend/src/features/agent/fixtures/*.json`                                                    |
| The recorder that builds them through the backend contracts                                                        | `scripts/record_agent_fixtures.py`                                                               |
| Client tests and backend tests over the same fixtures                                                              | `protocol/protocol.test.ts`, `backend/tests/agent/protocol/test_fixtures.py`                     |

## Guarantees

- The types cannot drift: a test regenerates them and fails when the committed file differs, and the generator reads the
  same schema file the backend tests pin.
- Each fixture validates, event by event, against the JSON Schema (envelope and the payload of its type), is a gap-free
  sequence for one session, and is byte-identical to what the recorder produces from the backend contracts.
- The backend reduces each fixture to the expected state: the normal run completes with one checkpoint; the rejected run
  marks the command denied and both approvals rejected and stays usable; the interrupted run reports the command as
  "outcome unknown", starts it once, and never runs it again.
- A client refuses an event of a protocol version it does not speak, and ignores event types it does not know.

## Verification

```
npm --prefix frontend run codegen:agent                      # regenerate after a protocol change
cd backend && uv run python ../scripts/export_agent_schema.py && uv run python ../scripts/record_agent_fixtures.py
npm --prefix frontend run typecheck && npm --prefix frontend run lint
npm --prefix frontend run test -- src/features/agent
cd backend && uv run --frozen pytest -c pyproject.toml tests/agent/protocol -q -o addopts=
```

## Dependencies added (dev only, pinned)

`ajv` 8.20.0 validates fixtures against the 2020-12 schema; `json-schema-to-typescript` 15.0.4 generates the types
(version 16 needs a newer `js-yaml` than the lint toolchain installs). Both are MIT.

## Not done here

The event store, reducer and fake agent, and every agent surface, come next.

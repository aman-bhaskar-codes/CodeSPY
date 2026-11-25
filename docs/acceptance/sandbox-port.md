# Acceptance: execution sandbox port

Scope: the boundary where uploaded source is edited and run, an in-memory implementation for tests, and the contract
suite every implementation must pass. No command is executed by this capability; a container-backed sandbox and a
hosted one implement the same port later. Decisions are in [ADR 0009](../adr/0009-execution-sandbox-tiers.md).

## What exists

| Part                                                                                          | Where                                  |
| --------------------------------------------------------------------------------------------- | -------------------------------------- |
| Port, canonical path rules, content and tree hashes, error types, limits                      | `backend/app/ports/sandbox.py`         |
| Contracts: spec, handle, revision, listing, slice, change, exec request and chunk, checkpoint | `backend/app/contracts/workspace.py`   |
| In-memory sandbox with scripted commands                                                      | `backend/app/adapters/sandbox/fake.py` |
| The shared contract suite and its fake-specific tests                                         | `backend/tests/agent/sandbox/`         |

## Rules the port enforces

- A sandbox belongs to one organisation and session; `acquire` re-attaches to a live one and returns a fresh, empty
  one after `release`. A handle is never authority: a forged, foreign or released handle is `SandboxGone`.
- Paths are canonical and workspace-relative: no absolute paths, `..`, empty or `.` components, backslashes, control
  characters, trailing slashes, over-long names, or any `.git` component.
- Writes are atomic batches of at most 200 changes. Each names the SHA-256 it expects (a create names none), so a
  stale edit is a `WorkspaceConflict` carrying the expected and actual hashes, never an overwrite. A rejected batch
  changes nothing. Each successful call advances the revision by exactly one; concurrent writers are serialised.
- Reads are line-based, bounded to 2,000 lines and 256 KiB, and refuse directories and non-text files.
- Commands are argv without a shell. The request refuses `LD_*`, `DYLD_*`, shell start-up and interpreter option
  variables and NUL bytes; the sandbox refuses a working directory outside the workspace and a timeout above its own
  limit. Output kept is capped at 4 MiB; a command always ends with exactly one `exit` chunk; a timeout reports
  `timed_out` with no exit code. A command is never re-run by the port.
- A checkpoint belongs to its sandbox and is checked against its content hash. Restoring reproduces the checkpoint's
  content exactly and still moves the revision forward.
- `suspend` keeps the workspace and the next call resumes; a sandbox that cannot suspend says so in its capabilities.

## Verification

```
cd backend
uv run --frozen pytest -c pyproject.toml tests/agent/sandbox -q -o addopts=
```

43 tests, no database or Docker. The contract suite (36 cases) was also run against a deliberately broken fake that
ignores expected hashes; three cases failed, which confirms the suite detects that fault.

## Next

The Docker sandbox ([docker-sandbox.md](docker-sandbox.md)) subclasses `SandboxContract` and passes the same cases. A
hosted sandbox does the same. The agent's tools are written against the port, so they run unchanged on the fake.

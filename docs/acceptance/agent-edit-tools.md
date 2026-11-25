# Acceptance: agent edit tools and the workspace record

Scope: the tools the agent uses to change files (`apply_patch`, `create_file`, `delete_path`, `move_path`), the durable
record of a session's workspace (revisions, checkpoints table), and the rules that make an edit safe to approve, apply and
repeat. Policy is in [ADR 0008](../adr/0008-agent-runtime-and-execution-policy.md); approval itself comes with the agent loop.

## What exists

| Part                                                            | Where                                                                                   |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Four edit tools, each split into plan and apply                 | `backend/app/services/agent/tools/edit_tools.py`                                        |
| Change sets as values: identity, post-image, "already applied"  | `backend/app/services/workspace/changes.py`, `apply.py`                                 |
| In-process workspace (tests, sessions with no record)           | `backend/app/services/workspace/local.py`                                               |
| Durable workspace service: open, apply, find, recover, announce | `backend/app/services/workspace/service.py`                                             |
| Tables, guards, grants (revision `0051_workspaces`)             | `backend/alembic/versions/0051_workspaces.py`, `backend/app/models/workspace_tables.py` |
| Tests, including PostgreSQL and real containers                 | `backend/tests/agent/tools/test_edit_*.py`, `backend/tests/agent/workspace/`            |

## The three guarantees

1. **A multi-file patch is atomic.** Every edit is planned against the files as they are, and every failure (a missing file,
   a search that is not found or ambiguous, a stale hash) is collected; if any fails, no file is written. A plan that is valid
   is written as one batch that the sandbox applies all-or-nothing, as one revision.
2. **A stale `expected_hash` returns a structured conflict with the current text.** The result has the error code
   `workspace_conflict`, the expected and actual hash in `data`, and up to 4 KiB of the file's current content centred on the
   edit (redacted), so the next call can quote the new hash. The conflict writes nothing, and covers a file changed by a
   command, by the user or by an earlier patch.
3. **Replaying an applied patch is a no-op.** Two mechanisms, both content-based:
   - the same tool request again is recognised by its digest and the workspace still holding its result, and returns
     "already applied" without reading or writing a file;
   - a saved plan applied again (as a restarted runner will) finds the sandbox already holding its post-image and writes
     nothing. If the first attempt died before recording the revision, the revision is recorded then, marked `recovered`.
     A patch overtaken by later edits is a conflict, not a silent second application.

## Other rules

- **Plan, then apply.** `prepare` returns the change set, per-file hunks and a redacted unified diff without writing, so what is
  approved is exactly what is written. Plan mode offers no edit tools; act mode adds them.
- **SEARCH/REPLACE is exact.** A search matches once, or names an `occurrence`; matching is exact including whitespace, with a
  hint of the closest existing line when it fails; CRLF files are edited with their line endings kept; an edit that changes
  nothing is refused.
- **Credential files are never edited, deleted or moved by the agent**, and results never carry secrets (diffs and excerpts are
  redacted; the file on disk is unchanged).
- **The record holds hashes, not content.** Each revision stores, per path, the hash before and after, the tree hash, the digest
  of the request and its author; revisions and checkpoints cannot be updated or deleted; a revision's number must equal the
  workspace head and a transaction that leaves a gap is rejected; a closed workspace takes no revisions.
- **The stream follows the record.** Each revision appends a `workspace.revision` event in the same transaction, once.
- **Files and directories cannot share a path** in either sandbox (a new contract case).

## Verification

```
cd backend
uv run --frozen pytest -c pyproject.toml tests/agent/tools -q -o addopts=             # edit tools, offline
# with Docker and a disposable PostgreSQL (see the other acceptance records):
CODESPY_SANDBOX_IMAGE=codespy-sandbox:dev CODESPY_TEST_SCHEMA_TARGET=head \
  uv run --frozen pytest -c pyproject.toml tests/agent -q -o addopts=
```

331 agent tests pass with both enabled. 55 are new here: 20 for `apply_patch`, 13 for the other edit tools and modes, 6 on real
containers (edits visible to the next command, executable bit and CRLF kept, a command's change conflicting with a stale edit),
14 on PostgreSQL (revisions, announcement, replay, recovery, conflicts, guards, grants, tools on the durable workspace).

## Not done here

- Checkpoint creation and restore (the `workspace_checkpoints` table exists and is guarded); that is the undo capability.
- User edits through the browser use the same service with `author: "user"` but have no route yet.
- The approval flow that calls `prepare`, parks, and then `apply`.

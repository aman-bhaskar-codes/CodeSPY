# Acceptance: agent read and search tools

Scope: the tools the agent uses to look at code without changing it: `list_dir`, `read_file`, `glob`, `grep`,
`find_symbol`, `search_semantic`, `git_status`, `git_diff`. They run over the sandbox workspace and the base snapshot;
nothing here edits a file or runs project code (`grep` is the only command, run in the sandbox). Policy is in
[ADR 0008](../adr/0008-agent-runtime-and-execution-policy.md).

## What exists

| Part                                                                                               | Where                                                                     |
| -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Result and spec contracts (`AgentToolResultV1`, `AgentToolSpecV1`)                                 | `backend/app/contracts/agent_tools.py`                                    |
| Truncation (head and tail kept), redaction that keeps line numbers, sensitive paths, glob matching | `backend/app/services/agent/tools/text.py`                                |
| Tool base, context, and the registry that validates and never raises                               | `tools/base.py`, `tools/registry.py`                                      |
| The eight tools                                                                                    | `tools/workspace_tools.py`, `tools/git_tools.py`, `tools/search_tools.py` |
| Tests, including real containers                                                                   | `backend/tests/agent/tools/`                                              |

The sandbox port gained `read_bytes` and `file_hashes` (both implementations and the contract suite), which the search and
change tools need.

## Guarantees

- **Every outcome is a result.** A bad argument, a refused path, a missing file, an unavailable service, a timeout or an
  unexpected failure each returns a status and a short safe message. Nothing is raised to the loop, and messages never echo
  argument values or internal detail.
- **Arguments are strict.** Unknown fields, wrong types (including `"2"` for an integer and `true` for a number), out-of-range
  values and oversized input are rejected before any tool runs.
- **Path traversal is denied.** Absolute paths, `..`, `.git`, backslashes, NUL and control characters are refused by every
  tool that takes a path or pattern, and the sandbox is never asked to open them.
- **Credential files are withheld.** `.env*`, keys, certificates and similar files are never read, listed with content,
  searched into results or diffed; the sandbox is not asked to open them.
- **Secrets are redacted.** Cloud keys, tokens, private-key blocks, hard-coded credentials and the canary token are replaced
  by markers in every field a tool returns, and `redactions` counts them. Line numbers do not shift when a multi-line secret
  is replaced.
- **Output is bounded.** Content is at most 32 KiB; when cut, the start and the end are kept and the marker says how many
  bytes were omitted. A read is at most 800 lines per call; a listing 2,000 files; grep 200 matches in 10 seconds.
- **Changes are measured against the snapshot, not a repository.** `git_status` and `git_diff` compare content hashes with the
  immutable base snapshot, so they need no git in the sandbox and a change that is undone leaves no status.
- **Stale indexes are flagged.** Symbol and semantic results in files changed this session carry a note that the index
  describes the base snapshot.

## Verification

```
cd backend
uv run --frozen pytest -c pyproject.toml tests/agent/tools -q -o addopts=
CODESPY_SANDBOX_IMAGE=codespy-sandbox:dev uv run --frozen pytest -c pyproject.toml tests/agent -q -o addopts=   # adds real containers
```

111 tool tests: 104 offline and 7 against real containers with real `grep` (literal and regex, case, globs, a leading dash,
binary files, an invalid expression, a catastrophic pattern). A canary test runs every tool over a workspace full of secrets
and asserts none appears in any serialised field.

## Not done here

- `find_symbol` and `search_semantic` are written against ports and tested with fakes; adapters over the existing structural
  facts and hybrid search are wired when the session runner is composed.
- `grep` needs a sandbox; the fallback to the snapshot's lexical index when no sandbox is bound is not built.
- Truncated output is not yet stored in the journal, so `content_ref` is empty.

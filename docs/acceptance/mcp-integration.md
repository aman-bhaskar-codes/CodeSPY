# Read-only MCP integration evidence

Reviewed on 28 October 2026 against HEAD
`6572c937b1869d495372c53059aade525ae1ae04` plus uncommitted local changes.
This record distinguishes implemented behavior, automated evidence and pending acceptance.
It does not establish a deployed service or manual desktop-host qualification.

## Implemented behavior

- The executable uses the official pinned `mcp==2.3.0` SDK low-level `Server` with
  explicit typed registration. The SDK owns protocol handling on the executable path;
  the older in-process compatibility helper is not the stdio transport.
- Thirteen read-only tools retain existing names and application service boundaries.
  Input/output schemas share one typed catalogue. Successful results carry structured
  content and an equivalent text representation; errors carry stable categories.
- Connected composition gives each call its own session, rechecks current authority
  before and after execution and supplies all retrieval/context/memory adapters.
  Missing source-reader configuration fails unavailable without a ZIP fallback.
- Direct evidence/graph reads bind the snapshot to the authorized repository and
  require published runs. Graph projections use the actual persisted column names,
  retain file identities and omit arbitrary stored properties. Evidence reports provider,
  digest and observation metadata while explicitly omitting arbitrary raw payloads;
  its metadata is not a claim of revalidated current source support.
- Saved investigation results and claims include current-support availability.
- Production composition supplies no embedding port or model gateway. Dense query
  coverage remains unavailable; lexical fallback is labelled explicitly.
- The actual executable pipe is bounded before SDK parsing: 64 KiB input frames,
  256 KiB serialized output frames. Result accounting includes both representations
  and reserves envelope space. JSON, identifiers and citation references are not cut.
- Two calls execute and four wait, with a 30-second total deadline including queue
  wait. Cancelled waiters release their counters. PostgreSQL startup checks connectivity
  and configures read-only transactions, statement/lock timeouts and a bounded pool.
- Source previews can compute the existing privacy transformation without inserting
  cache metadata. The normal API service default retains its existing cache behavior.
- Redacted telemetry excludes tool arguments and source/report bodies. Unknown tool
  names are sanitized. The executable suppresses unsanitized SDK diagnostics and
  diverts accidental stdout away from protocol frames.
- Configured isolated source reads reuse exact-owner container cleanup. No cloud
  clients, workers, schedulers, model calls, deployment or host registration are started.

The low-level SDK adapter preserves the other agent's implementation while exposing
explicit contracts; it does not introduce another protocol implementation or framework.

## Observed automated checks

Commands run from the checkout root:

```sh
.venv/bin/python -m pytest backend/tests/mcp backend/tests/phase5/unit/test_mcp_server.py backend/tests/investigations/unit/test_mcp_investigations.py backend/tests/retrieval/unit/test_index_mcp.py -c backend/pyproject.toml -o addopts='-ra --strict-markers' -q
.venv/bin/python -m pytest backend/tests/phase5/unit/test_source_view_service.py -c backend/pyproject.toml
.venv/bin/ruff check backend/app/mcp backend/app/contracts/mcp.py backend/app/services/source_view.py backend/tests/mcp
.venv/bin/mypy --config-file backend/pyproject.toml backend/app/mcp backend/app/contracts/mcp.py backend/app/services/source_view.py backend/tests/mcp/unit/test_mcp_catalogue.py
uv lock --check --offline
```

| Check | Observed result |
| --- | --- |
| MCP and compatibility suite | 117 passed (109 unit/integration/contract tests + 8 connected storage tests) |
| Existing source-view service suite | 5 passed |
| Connected PostgreSQL and native-reader suite | 8 passed; qualified against migration `0040_investigation_runtime_permissions` |
| Ruff on affected source and MCP tests | Passed |
| Strict mypy on affected production modules | Passed; 11 source files |
| Offline lock consistency | Passed; 208 resolved packages |
| Locked MCP dependency advisory audit | 27 packages checked; zero known vulnerabilities; none skipped |

The MCP checks cover SDK client calls, modern discovery without initialization, legacy
stdio initialization, tool schemas, real-process oversized framing, scope injection,
request-scoped composition, queue cancellation, SDK cancellation followed by a successful
call, safe source-cache behavior, actual graph-column compilation, response serialization
bounds, telemetry redaction, live PostgreSQL authority across all 13 tools, and real
subprocess pipe execution (`python -m app.mcp`) with runtime grant revocation.

The exact SDK requirement is reconciled in `uv.lock`. An isolated `pip-audit==2.9.0`
run against the 27 exact public registry packages in the locked MCP dependency closure
reported **zero known vulnerabilities, with zero packages skipped**. This is a dated
advisory check, not proof that those packages have no vulnerabilities. It does not cover
every dependency in the application or operating system.

macOS had offloaded virtual-environment files needed by schema and database test imports.
Affected packages were restored at their existing locked versions, without upgrades.
Lock consistency and the focused advisory audit are separate completed checks.

## Connected acceptance results

PostgreSQL 16 and Docker source-reader qualification were executed against owned, disposable
resources using the `scripts.phase2_db` harness and a verified reader container:
- Qualified local reader image: `sha256:a148a36093bbd946978f9af53956cc8de6844551c392d980abc840d7cc6fd879`
- Schema revision qualified: `0040_investigation_runtime_permissions`
- Separate database roles preserved: `p2_migration` (schema lifecycle), `p2_broker` (worker/finalization), `p2_api` (read-only MCP connection with `default_transaction_read_only=on`).

All eight connected storage cases in `backend/tests/mcp/storage/test_connected_mcp.py` passed:
1. `test_connected_search_context_memory_and_revocation`: Verified `search_exact`, `search_hybrid` (honest lexical fallback), `assemble_context`, `get_index_status`, `query_memory` (accepted review & clock expiry exclusion), `get_investigation_status`, `get_investigation_result`, `list_claims`, and grant revocation denial.
2. `test_repository_snapshot_mismatch_denied_by_direct_sql_adapters[get_evidence]`: Denies foreign snapshot evidence reads before data access.
3. `test_repository_snapshot_mismatch_denied_by_direct_sql_adapters[graph_neighbors]`: Denies foreign snapshot graph traversal before data access.
4. `test_cancelled_database_call_releases_connection`: Active slow query cancelled; connection returned to pool cleanly; subsequent query succeeds; background cancellation task safely awaited in `finally`.
5. `test_mcp_source_preview_uses_native_reader_and_writes_no_derived_cache`: Reads model-safe source via isolated Docker container and proves zero rows added to `derived_artifacts`.
6. `test_connected_findings_and_feature_trace`: Publishes scan findings, queries `list_findings` with current-support metadata, and executes `trace_feature` with bounded relationships.
7. `test_connected_structural_graph_and_evidence`: Publishes structural run and verifies `graph_neighbors` node/edge bounds and `get_evidence` metadata projection (raw payload safely omitted).
8. `test_subprocess_connected_with_real_postgres`: Real stdio subprocess (`python -m app.mcp`) connects to live PostgreSQL; handles `initialize`, `tools/list` (all 13 tools discovered), `search_exact`, `assemble_context`, `get_source_view` (via native reader), `get_investigation_status`, `list_claims`, denies calls after SQL grant revocation with structured `scope_denied` error, and terminates with exit code 0 on EOF.

Index preparation in these fixtures is synthetic and must not be represented as native index qualification.
Manual Codex desktop registration/discovery and fixture reads remain pending.

REST/OpenAPI/frontend contracts were not changed by these repairs, so frontend checks
were not rerun for this MCP-only change. Existing unrelated local repairs were preserved;
no GitHub delivery occurred.

## Acceptance status

Connected PostgreSQL and isolated reader qualification passed; manual desktop-host acceptance pending.
Remote HTTP, external MCP connections, mutating tools, and live dense-query
providers remain outside the enabled surface.

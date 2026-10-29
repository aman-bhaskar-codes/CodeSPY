# Durable findings

> **Status reconciliation — 28 October 2026:** Scoped scan configuration/start/status/cancel/retry, findings reads and triage UI now exist. See [workspace verification](frontend-workspace.md) and [findings/retrieval checks](retrieval-boundaries.md). The public-surface exclusions below describe the original storage qualification; activation still needs registered runtimes, catalogs and advisory inputs. No tests were rerun for this documentation update.

Internal scan jobs connect admitted, immutable source snapshots to isolated offline scanners and dependency assessment. At the time of this storage qualification, public scan submission and findings routes were disabled. No model, provider, cloud account or credential is required by this increment.

Reusable configurations are separate from run-bound attempt specifications. Seven additive tables retain configuration, attempt binding, reports, observations, finding occurrences, support and verification decisions. Jobs, outbox dispatch, leases, fencing, cancellation and recovery reuse the existing services. Manual retries retain the original requesting principal and require current write permission; another writer must create a new analysis.

Publication inserts the complete result in one transaction. Database guards check configuration-to-attempt binding, configured result accounting, source selections, coverage, immutable projections, evidence identities and terminal run/job associations. The total publication deadline is 60 seconds, including lock acquisition and commit. Individual statement and lock timeouts are 20 and 2 seconds. Entry requires the remaining lease/deadline to cover the remaining budget plus a 5-second margin. Cancellation rolls back or discards the connection. An uncertain commit is resolved by reading exact durable associations and hashes, without rerunning scanners.

The worker executes configured operations sequentially outside database locks. Its forward budget is at most 600 seconds and respects the remaining job deadline. Existing per-operation limits remain unchanged: native profiles allow 128 selected files and 16 MiB, and dependency assessment allows 32 metadata files and 2 MiB. Native tools remain limited to 1 GiB memory, two CPUs, 64 processes and 180 seconds. Publications allow at most 1,000 selected findings, 1,000 evidence records, 7,000 observation rows and a 4 MiB aggregate draft; these are independent ceilings, not a promise that every combination fits. Overflow is explicit partial coverage or rejection at the applicable boundary.

Reads recheck current repository permission and exact terminal associations. Historical decisions remain immutable. Current support can downgrade when retained source or exact advisory support becomes unavailable or stale; it cannot promote a historical unresolved decision. Advisory age is measured from its original fetched timestamp. Pure clock tests do not advance database lease authority.

The local capability registry accepts explicit pinned runtime, catalog and advisory identities. Crash recovery accepts marker-validated owner records; active executions are excluded from periodic cleanup. No global Docker pruning, image discovery, cloud access or public feature activation is introduced.

## Verification record

Verification uses synthetic archives and frozen advisory fixtures. Native qualification uses actual offline Semgrep CE 1.179.0 and Gitleaks 8.30.1 in image `sha256:ddc0aab8d60862f81fd1cfc6f43ce55e618497080f160fb56abe7e11c2ade6c2`. Its container-executed closure is unchanged; host-side orchestration, storage and SQL guards are tested separately. PostgreSQL 16 uses the existing pinned pgvector image and disposable, marker-owned databases with separate API, broker and migration roles. No developer database is used.

Observed on 21 October 2026 against the [recorded implementation and test source hashes](durable-findings-sources.json):

| Gate                                   | Observed result                            |
| -------------------------------------- | ------------------------------------------ |
| Complete owned PostgreSQL/native suite | 40 passed, no skips, 211.62 seconds        |
| Broad local Python regression suite    | 531 passed, 105 opt-in skips, 8.28 seconds |
| Ruff                                   | Passed                                     |
| Strict mypy                            | Passed, 149 application files              |
| Generated OpenAPI comparison           | Unchanged                                  |
| Whitespace/diff check                  | Passed                                     |
| Legacy persistence integration         | 21 passed, 2.54 seconds                    |
| Legacy migration compatibility         | 3 passed, 1.18 seconds                     |
| Legacy publication concurrency         | 4 passed, 0.80 seconds                     |

The 40-case gate covers actual admitted source → queue → both native tools plus matched advisory assessment → atomic publication → authorized read, retained-bundle loss, native 128-file/16-MiB ceilings for both tools, dependency 32-file/2-MiB ceiling, 1,000-finding SQL publication, scoped EXPLAIN row counts, actual database lease expiry, coordinated cancellation/revocation, exact replay, rollback, aggregate statement budget, lost commit acknowledgement and post-commit cancellation. Direct SQL regressions exercise omitted configured operations, wrong request/selection/coverage, immutable attempt configuration, incomplete publication, mutated payloads, duplicate evidence and sealed-record mutation. The 7,000-observation composite ceiling and exact 4-MiB payload boundary are enforced but were not separately saturated in the database/native suite.

The broad suite's skips are explicit image/database opt-ins; 40 owned storage/native cases run separately without skips. The remaining older opt-in fixtures were not rerun as a blanket full-system qualification. Synthetic receipt tests qualify transactions and SQL capacity, not native-tool execution. Timed results are local observations. Detailed command logs and the synthetic scoped-query EXPLAIN artifact stay in the ignored local work directory.

A single independent review identified two database-boundary defects: omitted configured results and altered attempt configuration. Both were reproduced with failing direct-broker regressions before hardening the SQL guards. A separate reproduced exact-set regression prevents duplicate report evidence from hiding extra stored records. Positive paths and regression gates must pass after these changes; wording alone does not qualify behavior.

Compatibility checks observed during this increment: 222 structural tests passed with three older image-dependent cases skipped; 44 ingestion/HTTP tests passed with one admission-image case skipped; two historical migration tests passed. These skips remain distinct from passes. The generated OpenAPI contract is unchanged. No frontend source is changed by durable storage, and frontend gates were not rerun for this increment.

## Boundary at the original qualification

At this original qualification boundary, public scan admission, findings HTTP/UI surfaces and triage were disabled. Later surfaces are linked above; runtime activation must inject qualified runtime, catalog, bundle and recovered-owner configuration explicitly. AWS access, provider keys, hosted execution, deployment and production load qualification remain outside this capability. Fixture timings are not production latency or peak-memory measurements.

Owned scan and legacy PostgreSQL containers, networks and volumes were removed through marker-validated cleanup. Final Docker inspection found no scanner-owned containers or containers from either test project. Both disposable credential/ownership files are gone. Docker itself remains under the owner's control. Implementation source hashes match the recorded evidence; Git HEAD is unchanged and all changes remain uncommitted.

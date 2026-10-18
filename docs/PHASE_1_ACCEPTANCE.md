# Phase 1 acceptance

Date: 18 October 2026. Starting commit: `04de7aa4fbb5807de9e409bf82229d86b888e3b4`. Delivery branch: `feat/phase1-database`. The final implementation and evidence are versioned together; use the pull request head for the delivered revision.

## Implemented scope

Twelve mapped tables, four independent Alembic revisions through `0004_guards`, explicit async session/transaction lifecycle, concrete scoped repositories, immutable DTOs, and atomic snapshot/result services. Findings, jobs, leases, events and embeddings are contract-only schemas under `docs/contracts/`. No ingestion, parser, scanner, identity flow, HTTP API, frontend, queue worker or hosted deployment is implemented.

A role-separated disposable PostgreSQL cluster was used. Developer `.env`, database and volumes were not used. No API keys, AWS account access, Chrome automation or external design workspace was required.

## Verified environment

macOS / Apple Silicon; Python 3.12.12. PostgreSQL 16.15 (Debian image), vector 0.8.7, pg_trgm 1.6, citext 1.6, uuid-ossp 1.1. Pinned container digest: `sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a`.

Locked packages retained: SQLAlchemy 2.0.54, Alembic 1.20.0, asyncpg 0.32.0, psycopg 3.3.6, Pydantic 2.14.1, pydantic-settings 2.16.0, pgvector client 0.5.1, pytest 9.1.1, pytest-asyncio 1.4.0, mypy 2.4.0, Ruff 0.17.1. `uv.lock` has no dependency changes.

## Executed checks

- `uv lock --check` and locked backend dependency tree: passed using a workspace-local uv cache.
- Unit suite: 31 passed; connection validation/redaction, strict Python vs JSON contracts, canonical JSON, ranges, paths, coverage and harness safeguards.
- PostgreSQL integration suite: 21 passed; publication/replay/rollback, scope keys, role checks, raw trigger/CHECK behavior, timeout/cancellation, cursor pagination and coordinated races.
- Migration suite: 3 passed; exact 12-table schema, one head, no metadata drift, trigger catalog, source-preserving upgrade and disposable downgrade/re-upgrade. Extensions remain installed.
- Performance suite: 1 passed; 100,000 nodes / 300,000 edges, with owner-refreshed statistics.
- Strict mypy: passed on 29 application modules.
- Ruff lint and formatting: passed on application, migrations, tests and Phase 1 scripts.
- Standalone demo: two nodes, one edge, two evidence records, one derivation; same-result replay, changed-result rejection, rollback, foreign-scope denial and stale-write rejection passed. Its database was removed after engine disposal. Fixture digests and sizes come from known UTF-8 source bytes, including CRLF and a Unicode identifier.

Commands are reproducible through `make phase1-unit`, `make phase1-test`, `make phase1-demo`, and `make phase1-performance` after the README preparation command. There are no SQLite substitutes or skipped integration checks in these counts. Concurrency cases are included in the integration count, not counted twice.

## Query plans

The dedicated corpus contains ten independent runs, each with 10,000 nodes and 30,000 edges (100,000 nodes / 300,000 edges total). Normal guards remain active; seed transactions use an explicit larger statement budget. The migration owner refreshes statistics with ANALYZE; the runtime role retains its restricted privileges. Acceptance permits index/bitmap/index-only variants and checks bounded results and filtering work without disabling sequential scans.

Final measured plans are saved in [phase1-query-plans.json](acceptance/phase1-query-plans.json). Timings are local observations, not a service objective or retrieval-quality measurement.

## Review and limitations

Independent review found three correctness issues. Regression tests first reproduced each failure, then passed after fixes: canonical publication must include all staged rows; SQL NULL cannot satisfy terminal coverage checks; cleanup can recover an owned cluster whose database marker was not yet provisioned. The implementation also tests edgeless graph cursor continuation.

Scope constraints prevent invalid associations, not unauthorized direct SQL reads. RLS and hosted authorization are deferred. Membership last-owner enforcement, retention/purge, source byte-to-line verification, durable jobs/leases/fencing, actual artifacts, retrieval and model qualification remain later work. A runtime role must never be exposed to untrusted users. Application health is read-only; extension provisioning remains administrative. Evidence identity is not claim verification.

The new workflow validates relevant pull requests/manual runs with disposable PostgreSQL only. It preserves the existing required check context and does not reactivate the old setup workflow, dependency schedules or cloud workflow.

## Handoff

Local test services are stopped at handoff, with the ownership file removed by the guarded cleanup command. Phase 2 must start from the accepted Phase 1 commit and introduce its own explicit migrations and behavioral tests. Existing Phase 0 history and unrelated local files are preserved.

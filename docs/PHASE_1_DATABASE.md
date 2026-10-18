# Local database operations

Phase 1 implements the persistence layer described in [ADR 0002](adr/0002-phase1-persistence.md). It has twelve application tables and four explicit Alembic revisions:

1. `0001_identity`: organizations, principals, memberships, repositories.
2. `0002_source`: artifacts, source snapshots, source files.
3. `0003_results`: runs, nodes, evidence, edges, derivations.
4. `0004_guards`: lifecycle, range, parent-lock and immutability guards.

## Transactions and identity

A service opens one fresh session and owns its transaction. Repositories do not commit. Snapshots seal their manifest atomically; runs publish a complete bounded result batch in one transaction. The published hash must describe the entire stored result set. A replay with different content fails. Run versions reject stale transitions; they are not worker leases.

Every owned association includes organization scope. Results also include snapshot and run scope. An identical local node ID can belong to different runs without overwriting results. Reads resolve membership and role through the database, then apply explicit scope and publication filters. Direct SQL with the application role is not read-isolated by tenant; no hosted access is supported.

Graph pages first return edges with their endpoint nodes. Typed cursors continue through remaining edges and then nodes, including disconnected nodes. Consumers merge repeated endpoint records by scoped identity. Neighbor queries have their own bounded edge cursor. A page is not a complete repository graph.

## Test environment

Use the README commands or `make phase1-unit`, `make phase1-test`, `make phase1-demo`, and `make phase1-performance`. Prepare the ignored `.phase1-test.env` first. The harness uses port 55432 and tmpfs storage; each suite gets a separate ownership-marked database. It refuses other hosts, ports, project labels, markers and database names. Cleanup verifies container ownership even when database provisioning failed partway through.

The test-only admin provisions extensions and roles. The migration role owns application tables; the application role has scoped operation privileges but no DDL, TRUNCATE, DELETE on historical results, or trigger bypass. It cannot run owner-only ANALYZE; the performance fixture deliberately uses the migration owner for that operation. Developer credentials and volumes are never test inputs.

## Deliberate development migrations

The harness is the recommended validation path. For a separately chosen local developer database, provide `DatabaseSettings` with an explicit dotenv path or database-only environment variables. No dotenv file is loaded automatically. `DATABASE_URL` takes precedence over complete `POSTGRES_*` fields; `DATABASE_MIGRATION_URL` can override the derived synchronous psycopg URL. Passwords are parsed as URL values, not interpolated into Alembic's ConfigParser.

Migrations require PostgreSQL 16 and existing `uuid-ossp`, `citext`, `pg_trgm`, and `vector` extensions. Provision them with an administrative identity before running migrations. No connection or migration automatically creates extensions. The harness provisions its own extensions separately and never drops them as an upgrade strategy.

```bash
uv run --frozen --all-packages --all-groups alembic -c backend/alembic.ini heads
uv run --frozen --all-packages --all-groups alembic -c backend/alembic.ini history
# Inspect the intended local destination and role before deliberately applying:
uv run --frozen --all-packages --all-groups alembic -c backend/alembic.ini upgrade head
uv run --frozen --all-packages --all-groups alembic -c backend/alembic.ini check
```

Do not stamp unknown tables or downgrade a developer database to match this plan. Destructive roundtrips are tested only in disposable databases. Accepted migrations become historical records; later changes require new revisions.

## Next-phase boundaries

Findings, jobs, attempt leases, progress events, and embedding profiles are contracts only. Phase 2 must add admission, sessions, jobs/outbox/leases/fencing and storage through new migrations. Source ranges record UTF-8 byte positions; this database validates bounds, not source-to-line correspondence. Evidence digests detect altered content, not truth. The vector check uses temporary three-element values and selects no production embedding profile or index.

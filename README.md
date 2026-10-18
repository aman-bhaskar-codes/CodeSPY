# CodeSPY

CodeSPY is a repository analysis project. The planned application will help developers explore unfamiliar code, inspect findings, and trace explanations back to source evidence.

The current implementation is the **local database foundation**. It stores organizations, repository snapshots, analysis runs, graph records, and evidence. It does not yet acquire repositories, analyze source code, serve an API, or provide a user interface.

## What works

- PostgreSQL schema with tenant, snapshot, and run constraints.
- Async persistence services with explicit transaction ownership.
- Immutable source manifests and atomic result publication.
- Content-based replay checks and stale lifecycle rejection.
- Four explicit Alembic revisions, a guarded test database, and a synthetic demo.

Evidence records describe their provider, location, basis, coverage, and limitations. Storing a record does not establish that a claim is correct. This local foundation is not a hosted authorization boundary; row-level security and public access controls remain future work.

## Run the database checks

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker with Compose v2. No API keys or cloud account are needed. The test harness starts only PostgreSQL on `127.0.0.1:55432`, using temporary storage and generated local test credentials. It does not read the developer `.env` or use its database volumes.

```bash
uv sync --all-packages --all-groups --frozen
make phase1-unit
uv run --frozen --all-packages --all-groups python scripts/phase1_db.py prepare --env-out .phase1-test.env
make phase1-test
make phase1-demo
uv run --frozen --all-packages --all-groups python scripts/phase1_db.py cleanup --env-file .phase1-test.env
```

Each integration suite gets a separate database created from migrations. Cleanup checks the test container's ownership marker and port binding before removing it. Keep the ignored ownership file if setup fails; it is needed for cleanup. Never commit it.

For the larger query-plan fixture, run `make phase1-performance` before cleanup. It measures selective reads over 100,000 nodes and 300,000 edges; it does not establish a general latency guarantee.

See [Phase 1 acceptance](docs/PHASE_1_ACCEPTANCE.md) for executed checks, measured plans, and limitations, and [database operations](docs/PHASE_1_DATABASE.md) for migration and role details.

## Layout

| Path               | Contents                                                             |
| ------------------ | -------------------------------------------------------------------- |
| `backend/app/`     | Contracts, database metadata, repositories, and persistence services |
| `backend/alembic/` | Historical database revisions                                        |
| `backend/tests/`   | Unit, PostgreSQL integration, migration, and query-plan tests        |
| `frontend/`        | Reserved React/TypeScript workspace; no application screens yet      |
| `scripts/`         | Local test harness, fixture, demo, and setup checks                  |
| `docs/`            | Plans, decisions, operations, and acceptance records                 |
| `infra/`           | Infrastructure configuration and owner-managed cloud notes           |

The root `uv.lock` is the Python workspace lock. `backend/uv.lock` links to it. The existing frontend lockfile is retained. Installed scanner, queue, and model packages are dormant until their phases require them.

## Phase 2 work in progress

The [revised execution plan](docs/phases/PHASE_2_SAFE_INGESTION_IDENTITY_JOBS_EXECUTION_PLAN.md) defines admission, identity and durable-job behavior. The first implementation contains additive schema, persistence helpers, ZIP inventory and local immutable storage. These internal components do not yet form an upload service. See the [implementation status](docs/phases/PHASE_2_IMPLEMENTATION_STATUS.md) for executed checks and remaining work.

## Next steps

The [technical blueprint](docs/CodeSPY_Technical_Execution_Blueprint.md) defines the current roadmap. Phase 2 adds safe admission, identity, durable jobs, and artifact lifecycle. Later phases add Python/JavaScript/TypeScript structure, deterministic findings, retrieval, investigations, and a workbench. Runtime verification, binary adapters, and hosted deployment have separate acceptance gates.

Phase 1 incorporates the blueprint's foundation reconciliation step. Each later phase starts from the accepted code and its own execution plan.

## Project boundaries

API keys and AWS account access are managed manually by the owner. Local database work does not require either. The existing all-service setup workflow and dependency schedules remain paused; the Phase 1 workflow validates database changes on pull requests or manual runs. Services are left stopped after local handoff.

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [MIT license](LICENSE)

# CodeSPY

CodeSPY is a repository analysis project. The planned application will help developers explore unfamiliar code, inspect findings, and trace explanations back to source evidence.

The current implementation supports local source ingestion and inventory, backed by PostgreSQL. It accepts bounded ZIP uploads, records immutable source snapshots, runs fenced jobs through an outbox and queue, and exposes a small API and upload/status interface. Phase 3 adds bounded structural analysis for Python, JavaScript and TypeScript, with source citations, graph projections, static framework observations and JSON/Markdown reports. AI explanations belong to later phases.

## What works

- Tenant-scoped persistence, source manifests and replay checks.
- Streaming uploads, Linux admission isolation and stored file identities.
- Durable jobs, retries, cancellation, recovery and staging cleanup.
- Session/CSRF boundaries and fixture-tested GitHub OAuth/App authorization.
- Typed OpenAPI contracts and a React upload, job, inventory and structural report interface.
- Isolated structural analysis with atomic report, graph and evidence publication.
- Bounded report sections, evidence excerpts, graph traversal, cancellation and guarded retries.

The supported archive limit is 100 MiB compressed and 10,000 entries, with additional expansion and resource bounds. See [Phase 2 acceptance](docs/PHASE_2_ACCEPTANCE.md) for test evidence and limitations. No hosted service or real provider credentials are required for local qualification.

## Run Phase 2 locally

Use Python 3.12, uv, Node 22 and Docker with Compose v2. The harness owns separate disposable PostgreSQL and LocalStack resources on loopback; it never reads a developer AWS profile.

```bash
uv sync --all-packages --all-groups --frozen
npm ci --prefix frontend
uv run --all-packages --all-groups python scripts/build_admission_image.py --image-out work/admission-image.id
uv run --all-packages --all-groups python scripts/phase2_db.py prepare --env-out work/phase2-test.env
make phase2-test
make phase2-capacity
make phase2-local-check
uv run --all-packages --all-groups python scripts/phase2_local.py --env-file work/phase2-test.env --image-file work/admission-image.id
# In another terminal: npm --prefix frontend run dev
# Stop the local API before removing the harness resources:
uv run --all-packages --all-groups python scripts/phase2_db.py cleanup --env-file work/phase2-test.env
```

The local command provisions a synthetic identity and repository in its owned database. Live sign-in, provider keys, AWS deployment and browser acceptance remain owner-managed or deferred. Keep the ignored ownership file until cleanup succeeds.

## Run structural analysis locally

Use a working Linux Docker runtime. The owner laptop's current Docker installation is unavailable; Linux CI provides container qualification. No services start automatically.

```bash
uv run --all-packages --all-groups python scripts/build_analysis_image.py --image-out work/analysis-image.id
uv run --all-packages --all-groups python scripts/phase3_db.py prepare --env-out work/phase3-test.env
CODESPY_ANALYSIS_IMAGE="$(cat work/analysis-image.id)" uv run --all-packages --all-groups python scripts/phase3_db.py test --env-file work/phase3-test.env
uv run --all-packages --all-groups python scripts/phase2_local.py --env-file work/phase3-test.env --image-file work/admission-image.id --analysis-image-file work/analysis-image.id
# Stop the API/worker before cleanup:
uv run --all-packages --all-groups python scripts/phase3_db.py cleanup --env-file work/phase3-test.env
```

Build the admission image using the Phase 2 command above first. Structural creation stays disabled until the image identity and five-dialect runtime probe pass. If the broker crashes, retain its exact owned workspace and run `scripts/cleanup_analysis.py --workspace <owned-analysis-root>` before removing that workspace. Never remove containers by name prefix.

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
| `frontend/`        | React upload, job, inventory and structural reports                  |
| `scripts/`         | Local test harness, fixture, demo, and setup checks                  |
| `docs/`            | Plans, decisions, operations, and acceptance records                 |
| `infra/`           | Infrastructure configuration and owner-managed cloud notes           |

The root `uv.lock` is the Python workspace lock. `backend/uv.lock` links to it. The existing frontend lockfile is retained. Installed scanner, queue, and model packages are dormant until their phases require them.

## Development status

Phase 2 ingestion, identity and durable inventory jobs are implemented locally. See the [acceptance record](docs/PHASE_2_ACCEPTANCE.md) for executed checks and deferred live-provider, browser and hosted gates.

Phase 3 is implemented and Linux-qualified for the bounded structural profile. See the [acceptance record](docs/PHASE_3_ACCEPTANCE.md) for commit-specific results and the [Phase 4 handoff](docs/PHASE_4_HANDOFF.md) for the next phase's inputs and limits. The original [readiness review](docs/engineering/phase3-readiness-review.md) is retained as the audit history.

The structural profile selects at most 128 eligible source files and 16 MiB of original source, independently of the upload limit. Reports contain at most 1,000 nodes, 2,000 edges and 2 MiB of JSON. Larger inputs produce explicit omissions or typed failures. Static candidates do not establish runtime behavior or code safety.

The [technical blueprint](docs/CodeSPY_Technical_Execution_Blueprint.md) governs later deterministic findings, retrieval, investigations and the workbench. Runtime verification, binary adapters and hosted deployment have separate gates.

## Project boundaries

API keys and AWS account access are managed manually by the owner. Local database work does not require either. The existing all-service setup workflow and dependency schedules remain paused; the Phase 1 workflow validates database changes on pull requests or manual runs. Services are left stopped after local handoff.

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [MIT license](LICENSE)

# CodeSPY

CodeSPY inspects source archives and builds bounded, source-linked structural reports for Python, JavaScript and TypeScript.

It records immutable snapshots, inventories files, runs isolated analyzers and publishes graph, evidence and report records together. A React interface supports uploads, job status, inventory, structural reports, cancellation and retries. Model-backed answers and vulnerability scanning are not enabled.

## Capabilities and limits

- Tenant-scoped persistence, source manifests and replay checks.
- ZIP uploads up to 100 MiB compressed and 10,000 entries, with path, expansion and resource checks.
- Durable jobs with outbox delivery, database leases, cancellation and recovery.
- Session/CSRF boundaries and fixture-tested GitHub OAuth/App authorization.
- Structural selection of up to 128 eligible files and 16 MiB of source; configuration has a separate 32-file/1-MiB budget.
- Reports capped at 1,000 nodes, 2,000 edges and 2 MiB of JSON, with explicit omissions and unavailable sections.
- Bounded graph traversal, verified source excerpts and JSON/Markdown exports.
- Metadata-only scanner contracts and opt-in admission of exact `uv.lock` metadata up to 256 KiB. These contracts do not activate scanners.
- Offline scanner adapters with authored Python rules and selected secret-format checks, authenticated archive staging, isolated range verification and durable container cleanup. Internal scan jobs use the existing durable queue and publication controls; public scan admission remains disabled.
- Typed findings materialization connects verified scanner results and offline advisory evidence to scoped occurrences, coverage and static verification decisions. Scoped PostgreSQL storage retains reports, observations, findings, support and verification decisions. Current-support reads preserve historical results; public findings routes remain disabled.
- Offline assessment of selected npm and uv lock metadata against frozen advisory evidence, with explicit unknown coverage and preserved package instances. This does not install target packages or contact providers.

Static candidates do not establish runtime behavior or code safety. A complete byte inventory does not imply complete structural coverage. See the [structural capability record](docs/capabilities/structural-small-v1.md) for qualification and limitations.

## Development setup

Use Python 3.12, uv, Node 22 and Docker with Compose v2. Container qualification requires a working Linux Docker runtime; service-free checks do not start containers or require provider keys.

```bash
uv sync --all-packages --all-groups --frozen
npm ci --prefix frontend
npm ci --prefix tools/typescript-analyzer
npm --prefix tools/typescript-analyzer run build
make unit
npm --prefix frontend test
npm --prefix frontend run build
```

The root `uv.lock` is the Python workspace lock. JavaScript packages have their own lockfiles. Local environments, source artifacts and credentials are excluded from Git.

## Disposable database checks

The harness uses generated local credentials, loopback ports and ownership-marked resources. It does not read a developer `.env` or reuse developer database volumes.

```bash
make database-prepare
make database-test
make database-demo
make database-performance
make database-cleanup
```

Keep the ignored ownership file if preparation or cleanup fails. Cleanup verifies ownership before removing resources. The query fixture measures 100,000 nodes and 300,000 edges; it is not a general latency guarantee.

## Ingestion and structural checks

Build trusted images and run one owned test cluster at a time:

```bash
make admission-image structural-image
make source-prepare
make ingestion-test
make scanner-contract-test
make structural-test
make local-demo
# Stop the local API/worker before removing its test resources.
make source-cleanup
```

The local demo uses synthetic identity and repository records. Structural creation stays disabled until image identity and a five-dialect runtime probe pass. After a broker crash, retain its ownership records and use `scripts/cleanup_analysis.py --workspace <owned-analysis-root>`. Never remove containers by name prefix.

Live GitHub sign-in, browser acceptance, model-provider integration and hosted deployment have separate qualification requirements. AWS access, paid calls, credentials and deployment require explicit owner authorization; provider secrets are owner-filled.

## Repository layout

| Path | Contents |
|---|---|
| `backend/app/` | Contracts, API, database metadata, services, repositories and workers |
| `backend/alembic/` | Immutable database revision history |
| `backend/tests/` | Unit, database, process-isolation and evaluation checks |
| `frontend/` | React upload, inventory and structural-report interface |
| `tools/typescript-analyzer/` | Isolated TypeScript compiler adapter |
| `scripts/` | Owned-resource harnesses, fixtures, demos and build utilities |
| `docs/` | Architecture decisions, operations, capabilities and qualification evidence |
| `infra/` | Container definitions and infrastructure configuration |

## Technical records

- [Architecture](docs/architecture/README.md) and [database operations](docs/database.md).
- [Persistence qualification](docs/acceptance/persistence.md), [ingestion qualification](docs/acceptance/ingestion.md) and [structural qualification](docs/acceptance/structural-analysis.md).
- [Scanner contract and admission qualification](docs/acceptance/scanner-admission.md).
- [Offline scanner integration evidence](docs/acceptance/offline-scanner.md).
- [Offline dependency assessment evidence](docs/acceptance/offline-dependencies.md).
- [Findings materialization evidence](docs/acceptance/findings-materialization.md).
- [Durable findings qualification](docs/acceptance/durable-findings.md).
- [Scanner artifact research](docs/engineering/scanner-qualification.md) and [REA investigation](docs/engineering/rea-investigation.md).

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [MIT license](LICENSE)

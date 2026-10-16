# Phase 0 service boundaries

| Component               | Phase 0 setup                                                                    | Later work                                                                  |
| ----------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Backend                 | Python 3.12 workspace, FastAPI/SQLAlchemy/SDK/parser/test dependencies installed | Application API, schema, workers, agents and graph implementation           |
| Frontend                | Node 22, React/TypeScript/Tailwind/editor/graph/test packages, configuration     | Workbench components, app entry point, user flows and application tests     |
| Database                | Local PostgreSQL 16 with vector, pg_trgm, citext and uuid-ossp                   | ORM models, migrations and query-plan validation                            |
| Cache                   | Local Redis 7.4, password, persistence, memory/eviction policy and healthcheck   | Cache algorithms, queues and SSE integration                                |
| Search                  | Local SearXNG JSON/HTML endpoint on localhost:18080                              | Search adapters and rate-limit handling                                     |
| Local emulator          | LocalStack 3.8.1, local artifact bucket, analysis queue/DLQ, placeholder secret  | Application integrations                                                    |
| Binary/firmware         | Ghidra, Rizin, JADX, WABT, Binwalk, ASAR, isolated Semgrep, containerized Unblob | Target adapters and restricted execution design                             |
| Model APIs / OAuth keys | Owner-managed, blank fields retained                                             | Owner manually configures credentials, quotas, access and current model IDs |
| Real AWS                | Access prohibited for the assistant                                              | Owner manually reviews existing bootstrap and performs future deployment    |
| GitBook / Miro / Figma  | Available for later                                                              | No new workspace/board/design now                                           |

The updated runbook selects Arq, replacing the earlier provisional Celery choice. The backend is a member of the root uv workspace; backend/uv.lock links to the canonical lock and backend/.venv is an ignored convenience alias. Frontend has an independent manifest/lockfile.

Only local test credentials are used for LocalStack. It is pinned to the runbook-compatible 3.8.1 release rather than a mutable latest image; container digests are committed. Firmware extraction is installed as an official Linux container on macOS, with networking disabled by its local wrapper.

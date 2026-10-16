# CodeSPY

Evidence-first agentic reverse engineering for source repositories, application bundles, and binaries.

**Status: setup only. No API, UI, analyzer, database schema, or cloud deployment has been implemented.**

The supplied Codeopsy plan is the architectural reference; CodeSPY is the requested product name.

## Intended architecture

- Python 3.12, FastAPI, Pydantic, async SQLAlchemy, Alembic, PostgreSQL 16/pgvector.
- React 19, TypeScript, Tailwind v4, TanStack Query, Monaco, Cytoscape/React Flow, SSE.
- Pure-Python CPG/DSA engine; Tree-sitter, Semgrep, Ghidra, Rizin, JADX, WABT.
- Claude/Gemini routing, four memory tiers, hybrid graph retrieval, evidence provenance and obligation ledger.
- Local PostgreSQL, Redis, SearXNG; future AWS ECS/RDS/S3/SQS and isolated analysis workers.

## Setup

1. Install Python 3.12 via uv and use Node 24.
2. Run `uv sync --frozen` and `npm ci --ignore-scripts`.
3. Run `uv run playwright install chromium` and `npx playwright install chromium` when browser tests are needed.
4. Copy `.env.example` to `.env` and generate independent random local passwords/secrets. Never paste API keys into chat.
5. Start Docker Desktop, then `docker compose up -d`. This starts dependencies only.
6. Run `uv run pre-commit install` inside this Git repository.

Local service addresses: PostgreSQL `127.0.0.1:5432`, Redis `127.0.0.1:6379`, and SearXNG `http://127.0.0.1:18080`. Credentials are local-only in ignored `.env`. `make stop` stops CodeSPY services without deleting database data.

See [setup status](docs/SETUP_STATUS.md), [complete plan mapping](docs/PROJECT_PLAN.md), [AWS onboarding](docs/AWS_SETUP.md), and [decisions](docs/adr/0001-setup-boundaries.md).

## License

License selection is pending owner confirmation. No open-source license grant is implied by the source plan or reference project.

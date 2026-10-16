# CodeSPY

**Evidence-first agentic reverse engineering for source repositories, application bundles, and binaries.**

[![Setup validation](https://github.com/aman-bhaskar-codes/CodeSPY/actions/workflows/setup.yml/badge.svg)](https://github.com/aman-bhaskar-codes/CodeSPY/actions/workflows/setup.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg)](backend/pyproject.toml)
[![React 19 + TypeScript](https://img.shields.io/badge/React-19%20%2B%20TypeScript-61DAFB.svg)](frontend/package.json)

**Phase 0: setup only.** No application API, ORM schema, DSA engine, agent workflow or workbench has been implemented. The capabilities below are the project roadmap, not claims of delivered software.

## Planned platform

CodeSPY aims to turn repository, bundle and binary evidence into traceable architecture and behavior explanations. Inspired by [morluto/rea](https://github.com/morluto/rea), the planned platform combines:

- A pure-Python Code Property Graph and DSA engine for syntax, control flow, data flow, call graphs and incremental indexing.
- Evidence provenance and a reconstruction obligation ledger distinguishing verified findings, static inference, runtime observation and unknown coverage.
- Hybrid graph retrieval with PostgreSQL/pgvector and lexical search; the exact BM25 approach remains an architecture decision.
- Configurable Claude/Gemini routing and working, episodic, semantic and procedural memory.
- Isolated adapters for source, Electron, Android, WebAssembly and native-binary analysis.
- An accessible React workbench, FastAPI REST/SSE API, and a later cloud demonstration.

Performance, recall, context-window and recovery targets require future measurement. See [architecture decisions](docs/adr/0001-setup-boundaries.md).

## Repository

| Path        | Responsibility                                                                 |
| ----------- | ------------------------------------------------------------------------------ |
| `backend/`  | Python package metadata; future API, analyzers, agents, graph engine and tests |
| `frontend/` | React/TypeScript package metadata, tooling configuration and future workbench  |
| `docker/`   | PostgreSQL extension initialization and SearXNG settings                       |
| `infra/`    | Owner-managed cloud/bootstrap documentation and provider configuration         |
| `scripts/`  | Local setup and read-only acceptance checks                                    |
| `docs/`     | Phase guides, decisions, toolchain and setup evidence                          |

The Python environment and canonical `uv.lock` are at repository root; `backend/uv.lock` points to the canonical workspace lock. Frontend packages have their own `frontend/package-lock.json`; the root npm package supplies documentation/convenience commands.

## Local setup

Prerequisites: Python 3.12 through uv, Node 22 through fnm, JDK 21, Docker with Compose v2, and the tools recorded in [TOOLCHAIN.md](docs/TOOLCHAIN.md).

```bash
source scripts/env.sh
make install
cp .env.example .env
# Set independent local PostgreSQL, Redis and SearXNG secrets in ignored .env.
make services
make bootstrap-local
make verify
```

Existing local credentials are preserved during updates. Never overwrite an existing `.env` with the example.

| Local service            | Address                  |
| ------------------------ | ------------------------ |
| PostgreSQL 16 + pgvector | `127.0.0.1:5432`         |
| Redis 7.4                | `127.0.0.1:6379`         |
| SearXNG                  | `http://127.0.0.1:18080` |
| LocalStack               | `http://127.0.0.1:4566`  |

LocalStack uses explicit dummy credentials and a loopback endpoint. It does not use a real AWS account. `make stop` stops this project's containers while preserving database volumes.

`make checks` runs dependency/configuration hygiene. `make verify` checks local tools, dependencies, database extensions, service health, local emulator resources and Chromium. Application build/tests will be exercised when application sources exist; their command definitions alone are not verification evidence.

## Credentials and account ownership

**All LLM/service API keys are configured manually by the owner.** Setup does not generate, retrieve or validate them. The owner also manages all AWS account access; the assistant must not open the account, sign in, or make real AWS API/CLI/connector calls. Blank template fields are deliberate.

A GitHub OAuth client was registered during earlier setup; its client secret was not generated. Cloud bootstrap changes made before the owner's stop instruction are recorded in [AWS_SETUP.md](docs/AWS_SETUP.md), without claiming a deployed application. GitBook, Miro and Figma are reserved for later.

## Roadmap

| Phase | Intended outcome                                                           |
| ----- | -------------------------------------------------------------------------- |
| 0     | Reproducible tooling, repository formalities and local dependency services |
| 1     | Database schema, constraints, async ORM and migrations                     |
| 2     | DSA/CPG engine and source/bundle/binary adapters                           |
| 3     | Hybrid graph retrieval, memory and evidence ledger                         |
| 4     | Multi-model routing, investigation DAG and tool integrations               |
| 5     | API/SSE, authentication, authorization and security hardening              |
| 6     | Accessible three-pane workbench                                            |
| 7     | Evaluation, optional specialist training and cloud portfolio demo          |

See [the project plan](docs/PROJECT_PLAN.md), [Phase 0 acceptance](docs/SETUP_STATUS.md), [contributing](CONTRIBUTING.md), and [security policy](SECURITY.md).

## License

[MIT](LICENSE), Copyright 2026 CodeSPY Contributors. Third-party tools retain their own licenses; their binaries and analyzed artifacts are not committed here.

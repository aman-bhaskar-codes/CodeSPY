# Services and dependency decisions

| Requirement       | Selected setup                              | Remaining requirement                                                            |
| ----------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| API and workers   | FastAPI, async SQLAlchemy, Celery           | No API or worker implementation                                                  |
| Local database    | PostgreSQL 16 + pgvector container          | Start Docker; extensions/migrations come later                                   |
| Lexical retrieval | PostgreSQL FTS planned                      | True BM25 implementation/extension decision                                      |
| Embeddings        | pgvector client                             | PDF does not choose embedding model, vector dimension, license, cost or provider |
| Local queue/cache | Redis container                             | Worker behavior and cache policy later                                           |
| Search            | SearXNG container, ddgs, Trafilatura        | Public search engine rate limits and reliability apply                           |
| Intelligence      | OSV.dev, deps.dev, GitHub APIs              | Integrations and upstream quotas later                                           |
| Claude and Gemini | anthropic and google-genai SDKs             | Keys, billing, model IDs, quotas, smoke test                                     |
| Authentication    | Authlib and JWT libraries                   | GitHub/Google OAuth registration and callback URLs                               |
| Tracing           | OpenTelemetry and Langfuse SDKs             | Choose self-hosted/hosted Langfuse; OTLP endpoint                                |
| Source parsers    | Tree-sitter + Python/JS/TS/Java/Go grammars | Other languages added only with adapter tests                                    |
| Native/binary     | Ghidra, Rizin, JADX, WABT, Semgrep          | Version verification and isolated execution                                      |
| IaC               | OpenTofu, Terraform-compatible HCL          | Account/region, state backend, provider lock and resource plan                   |
| Browser           | Python and Node Playwright                  | Chromium download and startup verification                                       |
| Secrets           | local ignored .env; future Secrets Manager  | Do not put credentials in repository or chat                                     |
| Fine-tuning       | Phase 7 isolated GPU environment            | No GPU dependencies or model weights installed on this Mac                       |

OpenTofu is used for the Terraform-compatible CLI to keep the setup open-source. No infrastructure resources are defined or applied in this setup phase. Container tags require digest locking once the Docker engine is repaired and image pulls can be verified.

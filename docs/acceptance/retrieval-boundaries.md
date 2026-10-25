# Findings and retrieval boundary checks

Qualification on 24 October 2026 used reference `83767b105f0eb0870c79f84402241d729084120b` plus the current uncommitted changes. The original checkout intermittently stalled on cached runtime reads. Tests ran from an exact public-reference reconstruction with individually hash-checked current-source overlays, Python 3.12.12 and the frozen dependency export. This is local evidence, not hosted readiness.

## Connected behavior

- Findings detail, filtering and summary use current retained-source/support state while preserving historical disposition. Bounded source-pattern excerpts pass through the isolated reader and whole-file privacy transform; secret-format findings do not expose excerpts. React renders returned text as text.
- Derived model-safe artifacts reject expired, unavailable or inconsistent registry rows. Coordinated concurrent first reads verify the same registered artifact rather than append contradictory identities.
- Exact, PostgreSQL lexical, dense and context services share current repository/snapshot/source authority checks. Hybrid search retains available exact/lexical results with explicit partial coverage when dense search is unavailable. PostgreSQL full-text weights use the required internal `"char"` type.
- Context assembly validates stored chunk metadata and counts actual model-safe bytes/tokens instead of trusting result estimates. A raw chunk digest is not a digest of transformed content; authenticated transformed-content provenance remains a separate requirement.
- Memory requires current shared repository authority, including viewer restrictions, GitHub grant expiration/revocation and active binding. Snapshot/source/evidence checks remain in force. Query matching precedes ordering and limits. These authorization regressions use synthetic SQLite data, not live GitHub or PostgreSQL lock qualification.
- Index publication rejects mixed scopes and incomplete/malformed embedding profiles/results. No embedding adapter is selected implicitly. External and lease-backed publication remain unavailable until a genuine durable index-worker binding exists; no external provider or budget dispatch occurs through this path.
- Unconfigured investigation execution returns unavailable before claiming a lease. Feedback cannot substitute repository identity for a missing investigation snapshot. Hybrid queries are limited to 256 characters; invalid memory query/limit values are rejected at the HTTP boundary.
- MCP dispatch validates bounded primitive arguments and uses sanitized errors. The gateway no longer performs an automatic second schema-repair dispatch against one reservation. These are mocked/offline boundaries, not provider or transport qualification.

## Observed results

- Combined backend selection: **866 passed, 7 skipped**. The skipped tests are the separately opted-in reader cases below. Selection: core, ingestion, structural, findings, retrieval, investigation unit suites, ingestion ports and reader tests, using `python -m pytest -c backend/pyproject.toml` with explicit paths.
- Separate immutable-image source reader: **7 passed**. Image and resource controls are recorded in [source reader acceptance](source-reading.md).
- PostgreSQL 16, isolated-reader and authenticated HTTP selection: **15 passed**. Includes concurrent derived-artifact registration, retention/revocation during reads, findings support views, triage replay/concurrency, migration compatibility and admitted-source exact/lexical/hybrid/context behavior. Chunk boundaries in the retrieval case are explicitly synthetic; this is not parser-to-index qualification.
- TypeScript sidecar: **11 passed** after building with the existing locked compiler.
- Frontend: **25 passed**; typing, lint and production build passed after API type regeneration.
- Strict application typing: **206 source files passed**. Ruff application checks passed. Focused authorization/publication/request regressions failed before their corresponding repairs and passed afterward.

A public tokenizer vocabulary was fetched over verified TLS and matched the SHA-256 pinned by the installed tokenizer library before offline regression. No model invocation, credentials, AWS access, provisioning, deployment or Git publishing occurred. Raw logs and source hashes stay in ignored local work files.

## Remaining integration

Durable source-to-index jobs, snapshot-bound durable investigations, real investigation tools and provider/budget/disclosure adapters remain incomplete. Gateway cancellation and settlement still need independent transaction durability; a request transaction is insufficient evidence. Standalone MCP initialization/transport/client interoperability, complete browser journeys, evaluation, observability and deployment remain unqualified. The current changes do not establish a complete deployed MVP.

# Runtime components

| Component | Responsibility |
|---|---|
| FastAPI | Authenticated commands, bounded reads and generated OpenAPI contracts |
| PostgreSQL 16 | Scoped identity, snapshots, graph/evidence, durable jobs, leases and publication authority |
| Broker/worker | Outbox dispatch, fenced attempts, cancellation, recovery and cleanup |
| Object store | Verified archives, manifests and bounded result objects; local and S3-compatible adapters |
| SQS-compatible transport | At-least-once wakeups; database state remains authoritative |
| Linux analyzer containers | Offline parser/compiler work with count, byte, time, memory and process limits |
| React interface | Uploads, status, inventory and source-linked structural reports |
| LocalStack | Disposable local storage/queue integration using explicit loopback endpoints and dummy credentials |

SDK and tool installation does not enable a runtime integration. Redis/Arq, external search, model providers and binary-analysis tools are not required for the source-analysis fixture journey. Offline scanner and dependency-assessment adapters are implemented behind typed ports; internal scan jobs publish through the existing PostgreSQL transaction and lease controls. Public scanner admission remains disabled.

The findings materializer consumes internal broker execution receipts, scoped requests and a reviewed claim catalog. It produces bounded occurrences, evidence references and verification decisions without database or provider I/O. A wire-valid result is not an execution receipt. Seven scoped tables retain reusable scan configurations, attempt specifications, reports, observations, occurrences, support and decisions. Every attempt binds the original principal and immutable configuration. The sequential worker uses injected runtimes, catalogs and advisory bundles. Public scan admission and findings routes remain disabled.

A 60-second total publication budget includes locks and commit; statement and lock timeouts are separate. Uncertain commits are resolved from durable state without rerunning tools. Read-time verification can downgrade historical support when source objects or retained advisory evidence are unavailable or stale; it cannot promote or rewrite history. Native and dependency operations retain their existing file, byte, memory, CPU and execution bounds. Crash cleanup accepts exact marker-validated owners and excludes active executions.

Secrets remain server-side and owner-filled. Local tests do not use an AWS profile. Services start only through explicit commands; owned test resources must be removed after verification.

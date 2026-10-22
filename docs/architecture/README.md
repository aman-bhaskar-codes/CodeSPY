# Source-analysis architecture

Web requests enter FastAPI, pass session/CSRF and repository authorization checks, and delegate to application services. Services own transactions; repositories issue scoped SQL; adapters implement storage, queue and provider boundaries.

Uploads produce immutable source snapshots and inventory records. Database outbox delivery schedules leased jobs. A broker authenticates stored archive/manifest/policy/file identities before handing generated read-only inputs to an offline Linux analyzer. The result validator checks original byte ranges and complete references; a fenced transaction publishes graph, evidence, report and terminal lifecycle together.

PostgreSQL time governs leases and publication. Queue receipts do not grant worker authority. Cancellation, replacement workers and ambiguous COMMIT recovery resolve through durable state. Source content is untrusted and is never executed or installed.

The React interface consumes generated API types and bounded reads. Structural coverage is separate from admission inventory. Unsupported inputs and omitted source remain explicit. Model-backed investigation, scanner execution and hosted operation are not enabled by dependency installation.

See [database operations](../database.md), [structural decisions](../adr/0003-structural-analysis.md) and [capability limits](../capabilities/structural-small-v1.md).

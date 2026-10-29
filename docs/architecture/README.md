# Source-analysis architecture

Web requests enter FastAPI, pass session/CSRF and repository authorization checks, and delegate to application services. Services own transactions; repositories issue scoped SQL; adapters implement storage, queue and provider boundaries.

Uploads produce immutable source snapshots and inventory records. Database outbox delivery schedules leased jobs. A broker authenticates stored archive/manifest/policy/file identities before handing generated read-only inputs to an offline Linux analyzer. The result validator checks original byte ranges and complete references; a fenced transaction publishes graph, evidence, report and terminal lifecycle together.

PostgreSQL time governs leases and publication. Queue receipts do not grant worker authority. Cancellation, replacement workers and ambiguous COMMIT recovery resolve through durable state. Source content is untrusted and is never executed or installed.

The React interface consumes generated API types and bounded reads. Structural coverage is separate from admission inventory. Unsupported inputs and omitted source remain explicit. Model-backed investigation, scanner execution and hosted operation are not enabled by dependency installation.

See [database operations](../database.md), [structural decisions](../adr/0003-structural-analysis.md) and [capability limits](../capabilities/structural-small-v1.md).

## Connected application services

Scoped scan commands and findings/triage reads use the same authorization and job
services. Runtime catalogs, advisory bundles and server-owned scan defaults are
injected; configuration availability is not a native-tool health probe.

Durable index jobs publish sealed chunk sets. Search, source context and reviewed
memory share retained-source and scope checks. Investigation commands enqueue work
through the existing jobs/outbox; the executor reuses checkpoint and encrypted
result stores. The model gateway owns dispatch authority and accounting. Uncertain
calls remain held for reconciliation rather than being treated as free retries.

Read-only MCP uses these existing application services under an operator-bound
identity and repository allowlist. It does not enqueue work, accept memory, invoke
models or expose shell/SQL execution. The React workbench uses focused hash panes,
generated transport contracts and current server authority.

The [workspace record](../acceptance/frontend-workspace.md),
[index interface evidence](../acceptance/index-interface.md),
[investigation evidence](../acceptance/investigations.md) and
[MCP evidence](../acceptance/mcp-integration.md) distinguish implemented connections
from executed checks. Live provider quality and the complete single
upload-to-saved-result browser journey remain unqualified. The owner handles cloud
services, secrets and deployment; no hosted environment is implied by this design.

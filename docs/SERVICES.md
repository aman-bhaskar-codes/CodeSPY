# Runtime components and configuration

Installed SDKs and visible controls do not activate services. CodeSPY uses explicit
ports and composition; a missing runtime is an unavailable capability, not permission
to discover credentials, create accounts or fall back to a paid provider.

## Components

| Component               | Responsibility                                                                | Configuration boundary                                                                        |
| ----------------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| FastAPI                 | Sessions, CSRF, scoped commands and bounded reads                             | `ApiSettings` and `ApplicationPorts` are explicitly injected                                  |
| PostgreSQL 16           | Source identity, jobs, leases, reports, evidence, memory and accounting       | Separate API, broker and migration authority; database time governs leases                    |
| Object store            | Retained verified archives and bounded objects                                | Explicit local or S3-compatible adapter; credentials are owner-managed                        |
| Queue and worker        | Outbox delivery, fenced claims, recovery and cleanup                          | Queue adapter and registered operations; a message is not publication authority               |
| Isolated Linux runtimes | Admission, structural analysis, scanners, source reading and indexing         | Trusted image identity and bounded execution; never execute uploaded application code         |
| React workbench         | Intake, files, reports, triage, retrieval, reviewed memory and investigations | Generated transport contracts, scoped selection and explicit unavailable states               |
| Model gateway           | Request preparation, dispatch authority, usage settlement and reconciliation  | Disabled profiles until explicit owner configuration, prices, funding and disclosure approval |
| Read-only MCP           | Bounded inspection through existing scoped services                           | Explicit identity, repository allowlist and database/source-reader configuration              |
| LocalStack              | Disposable local S3/SQS-compatible integration                                | Explicit loopback endpoints and dummy test credentials; not an AWS account                    |

Redis/Arq, external search services and binary-analysis tooling are not implicitly
part of these source-inspection workflows.

## Configuration ownership

| Setting or injected port                                         | Purpose                                                             | Missing or disabled behavior                                                                    |
| ---------------------------------------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `ApplicationPorts.scan_defaults`                                 | Server-owned reusable scanner configuration                         | Scan configuration is unavailable; the browser cannot choose arbitrary runtime/profile settings |
| `ApplicationPorts.scan_capabilities`                             | Registered runtimes, catalogs, bundles and validation               | Unqualified scanner selections fail closed                                                      |
| `ApplicationPorts.source_reader`                                 | Isolated retained-source reads and safe excerpts                    | Source-dependent reads/support cannot claim availability                                        |
| `ApplicationPorts.model_runtime`                                 | Configured generation gateway and accounting                        | Without a runtime or explicit fixture adapter, execution returns `inference_unavailable`        |
| `build_worker_runtime(..., structural=..., scan=..., index=...)` | Register existing worker operations                                 | A frontend control alone does not enable processing                                             |
| `propose_report_memory`                                          | Opt-in report-to-candidate consumption                              | Disabled by default; accepted knowledge still requires human review                             |
| `MemoryContextSettingsV1`                                        | `disabled`, `shadow` or `reviewed` memory selection                 | Default disabled; shadow does not disclose memories to providers                                |
| `TIKTOKEN_CACHE_DIR`                                             | Verified tokenizer vocabulary for offline tests/runtime preparation | Missing cache is an environment issue, not proof of index publication failure                   |

The [model runbook](operator-model-runbook.md) describes slot profiles, admission
flags, encryption, provider transports and disclosure. The
[budget runbook](operator-budget-runbook.md) covers owner funding and audited
reconciliation. The [MCP runbook](operator-mcp-runbook.md) lists its actual environment
variables and transport limits. No guide should require copying keys into public docs.

## Operational behavior

Scan configuration, scoped lifecycle commands and published-run findings are wired
through the existing API/workbench. The sequential worker uses injected native
runtimes, reviewed catalogs and retained advisory bundles. No native scanner health
probe is performed by the configuration endpoint. The [workspace record](acceptance/frontend-workspace.md)
separates synthetic registry HTTP checks from native scanner qualification.

Scan publication uses a 60-second total budget including locks and commit; statement
and lock limits are separate. Durable replay resolves uncertain commits without
rerunning tools. Read-time support can downgrade unavailable/stale evidence without
rewriting historical decisions. Limits are recorded in [durable findings](acceptance/durable-findings.md).

Model cancellation and stale-call recovery preserve uncertain usage holds; neither
is an automatic refund. Token streaming and live provider access remain separate
qualification gates. Model availability is sanitized metadata, not a provider call.

## Local setup, checks and cleanup

Use the [README](../README.md#local-development) to install trusted dependencies and
run offline checks. The local intake/structure demo is a subset, not an all-services
launcher. The application factory requires injected settings and ports; there is no
zero-argument production launch command documented here.

Run Docker checks only through their explicit owned-resource harness. Retain the
generated ownership manifest until cleanup succeeds. Stop owned workers before
removing their database/network; preserve unrelated services and developer volumes.
See [database operations](database.md) and [investigation operations](operator-investigation-runbook.md).

Current local results are linked from [workspace verification](acceptance/frontend-workspace.md),
[index interfaces](acceptance/index-interface.md) and [MCP evidence](acceptance/mcp-integration.md).
These are recorded results, not tests executed by this documentation update.
The complete single upload-to-investigation browser journey, live model quality and
hosting remain unqualified. The owner handles AWS, credentials and deployment.

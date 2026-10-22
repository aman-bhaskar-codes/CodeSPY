# ADR 0003: Bounded structural analysis and toolchain qualification

Status: accepted architecture; integrated repair implemented, Linux qualification passed at `27fbde4` in run `39360366079`. The [acceptance record](../acceptance/structural-analysis.md) owns release evidence. Initial toolchain qualification baseline: `736a5ef831ed4a81899916153a7d09a13a51f2d1`.


## Decisions

1. Retain the Python control plane, PostgreSQL time and authority, object-store boundary, outbox/SQS jobs, cancellation, immutable snapshots, persisted file identities and original persistence APIs. OpenTofu remains the accepted infrastructure direction. No new cloud service or agent framework is introduced.
2. Start with `structural-small-v1`: Python and JS/JSX/TS/TSX, at most 128 selected eligible sources and 16 MiB original bytes. Admission remains independently limited to 10,000 entries. Structural output keeps the existing 1,000-node/evidence and 2,000-edge/derivation publication caps; canonical result/report budgets are 8/2 MiB. Qualification must measure the advertised source profile; limits alone are not a throughput claim. See the capability record for measured results.
3. Pin Tree-sitter 0.26.0 with direct Python 0.25.0, JavaScript 0.25.0 and TS/TSX 0.23.2 grammars. Keep language-pack dormant: analysis must not trigger grammar downloads. Store Python resolution in the root `uv.lock`; use a separate TypeScript 5.9.3/Node 22 package lock. No existing resolved Python package version changed during qualification.
4. Use Tree-sitter for syntax, Python 3.12 ast/symtable for a bounded lexical supplement, and an isolated virtual-host TypeScript checker for bindings. Never execute target code/configuration, import target modules, install target dependencies, load plugins or fall back to host node_modules. Missing context produces explicit unknowns.
5. Native work runs behind the qualified Linux process/container boundary. **Do not use the 0.26.0 progress-callback API**: the documented two-argument form terminated a trusted child with SIGSEGV on both macOS arm64 and Linux arm64 under Python 3.12.12. Bytestring parsing passed. External per-operation and process-tree deadlines own cancellation; a Python callback is not the resource boundary. No upstream patch or silent upgrade was applied.
6. Add explicit structural job/config/run registration and V3 transport while preserving historical V2 operations. A broker-owned source locator and isolated reader must authenticate archive, manifest, original policy and stored file IDs before parsing. The broker locator and owned local runtime now compose these boundaries.
7. Keep bounded atomic publication. The structural finalizer atomically publishes report, complete graph/evidence, terminal run/job and event under existing fences and aggregate 60-second budget including locks and awaited COMMIT. Preserve statement/lock/idle distinctions, rollback/connection recovery and durable-state reconciliation after ambiguous COMMIT.
8. Use typed binding/framework/report records instead of invented graph node kinds. Resolved lexical binding is distinct from runtime call targets. Routes/requests require explicit service/origin evidence for candidate links. Same names or paths never establish a unique cross-service relationship.
9. Implement small deterministic iterative graph algorithms with work budgets; use NetworkX as a differential reference. An interrupted SCC section is unavailable, not a complete decomposition of an undisclosed prefix. Full rebuild precedes caches, CSR or incremental analysis.
10. Compute semantic matching keys separately from scoped physical IDs and normalized report digest. Preserve original UTF-8 bytes, CRLF/BOM and explicit half-open ranges. Keep evidence and omitted/ambiguous/unsupported coverage visible. No source text in default logs.
11. Reuse the current API shell, authorization, generated OpenAPI/TypeScript tooling and React interface. Creation and retry routes are explicitly runtime gated. Source navigation uses bounded verified excerpts, not API-side arbitrary archive paths. Browser, live-provider and hosted qualification remain separate.
12. Follow the ordered migration and evaluation gates: new revisions after 0009, original 0004/12 and 0009/29 catalogs retained at their revisions, new-head behavior tested separately. Freeze hand-labelled fixture splits/thresholds before evaluation. No universal accuracy or throughput claim follows from tiny qualification fixtures.

## Inventory coverage reconciliation

The implemented `JobFinalizer.inventory_batch` reports **complete byte inventory**: every admitted regular file has metadata and a digest. Its eligible/excluded counts are separate evidence payload fields; exclusions do not make byte inventory incomplete. Existing result hashes, runs and histories remain unchanged.

Structural coverage is operation-specific: inventoried files may be unsupported, excluded, out of selected scope or omitted by structural budgets. Complete byte inventory never implies complete semantic analysis or safe code.

## Qualification and consequences

The [acceptance record](../acceptance/structural-analysis.md) and [capability record](../capabilities/structural-small-v1.md) distinguish current qualification from the original toolchain qualification probes. The integrated implementation includes source authentication, offline syntax/compiler work, atomic report/graph/evidence persistence, scoped bounded APIs and React reports. The final repair passed 225 structural analysis checks and 44 ingestion integrations at structural head (one separate admission-image check skipped); hosted and browser checks remain separate.

Coverage detail is bounded by the final 2-MiB report budget. `omitted_details_count` plus the listed omissions equals the exact omitted-file count; full inventory remains authoritative. Deterministic whole-file omissions preserve referential closure and never trim alternatives into a falsely unique candidate. This additive structural analysis DTO change requires generated clients; historical persistence and ingestion contracts and migrations are unchanged.

Compiler transport is independently capped at 20 MiB after JSON escaping. Exceeding it retains syntax and marks resolution unavailable/partial; source/config limits are not increased. Package metadata is data, not an extra compiler project. Parser subprocesses have an external deadline and container-wide memory enforcement.

Request replay includes principal identity. Manual retry preserves the original spec principal and requires current write access; another writer creates a new spec. No impersonation, terminal-run reopening or historical-spec mutation is allowed.

The important upstream cancellation limitation remains contained through external process lifecycle control. Another API/version requires an explicit pin change and fresh qualification. The runner persists exact ownership before container creation, supervises bounded readers and preserves failed-cleanup records for recovery. No Docker prefix deletion is permitted.

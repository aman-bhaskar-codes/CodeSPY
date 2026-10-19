# ADR 0003: Bounded structural analysis and toolchain qualification

Status: WP-3.00 approved and qualified locally on 19 October 2026. The structural subsystem remains proposed and disabled until its dependent work packages pass. Starting commit: `736a5ef831ed4a81899916153a7d09a13a51f2d1`.

The owner authorized agent implementation of the [Phase 3 plan](../phases/PHASE_3_STRUCTURAL_ANALYSIS_EXECUTION_PLAN.md), beginning with WP-3.00. The plan's manual implementation language does not require Aman to type the changes. Existing AWS, real-key, deployment and browser-session restrictions remain.

## Decisions

1. Retain the Python control plane, PostgreSQL time and authority, object-store boundary, outbox/SQS jobs, cancellation, immutable snapshots, persisted file identities and original Phase 1 APIs. OpenTofu remains the accepted infrastructure direction. No new cloud service or agent framework is introduced.
2. Start with `structural-small-v1`: Python and JS/JSX/TS/TSX, at most 128 selected eligible sources and 16 MiB original bytes. Admission remains independently limited to 10,000 entries. Structural output keeps the existing 1,000-node/evidence and 2,000-edge/derivation publication caps; canonical result/report budgets are 8/2 MiB. These structural capacities are design limits, not measured analyzer capabilities.
3. Pin Tree-sitter 0.26.0 with direct Python 0.25.0, JavaScript 0.25.0 and TS/TSX 0.23.2 grammars. Keep language-pack dormant: analysis must not trigger grammar downloads. Store Python resolution in the root `uv.lock`; use a separate TypeScript 5.9.3/Node 22 package lock. No existing resolved Python package version changed during qualification.
4. Use Tree-sitter for syntax, Python 3.12 ast/symtable for a bounded lexical supplement, and a later isolated virtual-host TypeScript checker for bindings. Never execute target code/configuration, import target modules, install target dependencies, load plugins or fall back to host node_modules. Missing context produces explicit unknowns.
5. Native work runs behind the later WP-3.02 process boundary. **Do not use the 0.26.0 progress-callback API**: the documented two-argument form terminated a trusted child with SIGSEGV on both macOS arm64 and Linux arm64 under Python 3.12.12. Bytestring parsing passed. External per-operation and process-tree deadlines own cancellation; a Python callback is not the resource boundary. No upstream patch or silent upgrade was applied.
6. Add explicit structural job/config/run registration and V3 transport while preserving historical V2 operations. A broker-owned source locator and isolated reader must authenticate archive, manifest, original policy and stored file IDs before parsing. These are later packages; adding an operation string alone is insufficient.
7. Keep bounded atomic publication. The later structural finalizer must atomically publish report, complete graph/evidence, terminal run/job and event under existing fences and aggregate 60-second budget including locks and awaited COMMIT. Preserve statement/lock/idle distinctions, rollback/connection recovery and durable-state reconciliation after ambiguous COMMIT.
8. Use typed binding/framework/report records instead of invented graph node kinds. Resolved lexical binding is distinct from runtime call targets. Routes/requests require explicit service/origin evidence for candidate links. Same names or paths never establish a unique cross-service relationship.
9. Implement small deterministic iterative graph algorithms with work budgets; use NetworkX as a differential reference. An interrupted SCC section is unavailable, not a complete decomposition of an undisclosed prefix. Full rebuild precedes caches, CSR or incremental analysis.
10. Compute semantic matching keys separately from scoped physical IDs and normalized report digest. Preserve original UTF-8 bytes, CRLF/BOM and explicit half-open ranges. Keep evidence and omitted/ambiguous/unsupported coverage visible. No source text in default logs.
11. Reuse the current API shell, authorization, generated OpenAPI/TypeScript tooling and React interface. Later routes are explicitly feature gated. Source navigation uses bounded verified excerpts, not API-side arbitrary archive paths. Browser, live-provider and hosted qualification remain separate.
12. Follow the ordered migration and evaluation gates: new revisions after 0009, original 0004/12 and 0009/29 catalogs retained at their revisions, new-head behavior tested separately. Freeze hand-labelled fixture splits/thresholds before evaluation. No universal accuracy or throughput claim follows from tiny qualification fixtures.

## Inventory coverage reconciliation

The implemented `JobFinalizer.inventory_batch` reports **complete byte inventory**: every admitted regular file has metadata and a digest. Its eligible/excluded counts are separate evidence payload fields; exclusions do not make byte inventory incomplete. Existing result hashes, runs and histories remain unchanged.

The old Phase 2 plan incorrectly required eligible-only processed counts and partial status whenever exclusions existed. Correct that documentation to the accepted implementation. Phase 3 will have operation-specific structural coverage: inventoried files may be unsupported, excluded, out of selected scope or omitted by structural budgets. Complete byte inventory never implies complete semantic analysis or safe code.

## Qualification and consequences

See the [capability record](../capabilities/structural-small-v1.md) and [REA reference investigation](../engineering/phase3-rea-investigation.md). Qualified: tiny grammar/ABI/query/range checks, compiler fixtures and an owned offline Linux probe with basic cgroup, bounded-output and cancellation cleanup checks. The production analyzer, adversarial isolation, complete virtual host, accuracy corpus and maximal structural capacity remain unimplemented or unqualified. No structural job, API or database migration is enabled by this ADR.

The important upstream cancellation limitation is contained through external process lifecycle control. If another API/version is adopted later, it needs an explicit pin change and new qualification rather than an assumption that a documented signature is safe.

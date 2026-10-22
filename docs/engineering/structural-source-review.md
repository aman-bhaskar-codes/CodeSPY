# Structural source review: historical defects and repairs

Inspected commit: `e98554509faeb8ed74d8c40bcb3fc1eec8ece5c4`, 19 October 2026. This record describes defects at that commit. It is not a list of current open defects; [structural qualification](../acceptance/structural-analysis.md) records the subsequent repairs.

| Original source observation | Repaired behavior and regression boundary |
|---|---|
| A worker query referenced `source_files.c.repository_id`, which does not exist. | Resolve repository scope through the snapshot and persisted inventory/file identities; reject cross-snapshot input. |
| Synchronous analysis ran in the async broker, and the local consumer omitted structural operation wiring. | Inject an isolated analyzer runner; qualify memory, network, deadlines, cancellation and heartbeat responsiveness. |
| The finalizer inserted a report without publishing its graph/evidence through the persistence service. | Validate and materialize one bounded complete result, then publish graph, evidence, report and terminal state atomically under the worker fence. |
| Source lookup guessed a default policy and omitted language/object-version identity. | Authenticate original archive, manifest, admission policy, object version and stored file metadata before parsing. |
| API configuration used an all-zero toolchain hash without runtime readiness gating. | Require a qualified configured toolchain and an available runtime before accepting structural work. |
| Request replay generated a fresh spec UUID before checking stable request semantics. | Resolve request identity before allocation; test identical sequential/concurrent replay and conflicting input. |

The missing-column failure was directly reproduced at the inspected commit. Other original findings were source observations; they were not labelled executed integration failures. The original unit run passed 172 tests and did not establish the missing end-to-end authority/isolation behavior.

Subsequent qualification covers PostgreSQL publication/recovery, exact-source reconstruction, five supported parser dialects, bounded native execution and source-capacity fixtures. Hosted isolation, live provider behavior and manual browser acceptance remain separate from these source-analysis checks.

# Phase 3 readiness review — historical baseline

This audit is retained as evidence of the original defects. Current repair status and release qualification are recorded in [Phase 3 acceptance](../PHASE_3_ACCEPTANCE.md). The findings below describe the inspected baseline, not the repaired branch.

Reviewed on 19 October 2026 against `e98554509faeb8ed74d8c40bcb3fc1eec8ece5c4` on `main` and the committed Phase 3 execution plan. This review changes documentation only. It does not accept Phase 3 or start Phase 4.

## Decision

**Phase 4 implementation is blocked.** Analyzer and UI components exist, but the delivered worker-to-database path does not satisfy Phase 3 publication and isolation contracts. Passing component checks cannot establish those contracts.

## Findings and required regressions

| Priority | Source observation                                                                                                                                                                                                     | Required correction and regression                                                                                                                                                                                                                                                                            |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Blocking | `backend/app/workers/operations/analyze_structure.py:133` references `source_files.c.repository_id`; that table has no such column.                                                                                    | Scope through the snapshot and persisted file/inventory identities. Run a real structural job against the current migrated database, including cross-snapshot rejection.                                                                                                                                      |
| Blocking | `analyze_structure.py` invokes synchronous `analyze()` directly in the async broker. The local harness constructs `JobConsumer` without its optional structural operation.                                             | Wire a qualified, isolated analyzer process/container into an owned local runtime. Prove network/credential isolation, memory/output/time bounds, heartbeat responsiveness and cancellation. Toolchain probes alone do not qualify this production path.                                                      |
| Blocking | `backend/app/services/structural_finalization.py` inserts a report and directly updates the run/job; it never calls the Phase 1 persistence helper or inserts graph/evidence batches.                                  | Validate the draft against verified source, materialize scoped graph/evidence identities and publish the bounded result batch in the same fenced transaction. Test durable report/graph/evidence consistency, lease expiry, cancellation, revocation, rollback and ambiguous commit recovery with PostgreSQL. |
| Blocking | The source worker falls back to archive inspection with a default admission policy, omits language from stored-file construction, and does not carry the archive object version into its descriptor.                   | Resolve the verified available archive and original admission manifest/policy from immutable ledger state, preserving file metadata and version identity. Test uploaded and root-stripped GitHub archives, replay and mismatched metadata.                                                                    |
| Blocking | `backend/app/api/application.py:795` creates analysis configuration with an all-zero toolchain hash. Structural submission is not gated on a qualified configured runtime.                                             | Use the actual qualified toolchain identity and a readiness/feature gate. Test rejection when disabled or unavailable and successful submission only with matching configuration.                                                                                                                             |
| High     | `StructuralAnalysisService.create_analysis_spec` allocates a new spec UUID before each enqueue. The enqueue request digest includes this UUID, so an identical request-key replay conflicts with its previous request. | Resolve idempotency using stable request semantics before generating a new spec; preserve immutable specifications. Test sequential and concurrent identical replay and conflicting input.                                                                                                                    |

The missing-column failure was reproduced without services by importing `source_files`, listing its columns and accessing `source_files.c.repository_id`: SQLAlchemy raised `AttributeError: repository_id`. Other findings above are source observations requiring implementation and regression execution, not claims of executed integration failures.

## Verification and limitations

- Fresh review run: `backend/tests/phase3/unit` passes (172 tests). These checks do not cover the real integrated path identified above.
- GitHub inspection found successful Phase 1/2 foundation workflows at the reviewed head. No dedicated Phase 3 qualification workflow was present. A generated backend dependency-graph run at the merge commit failed; this review did not diagnose or change that run.
- Live Phase 3 PostgreSQL publication/race tests, production analyzer isolation, maximum-profile benchmarks, labelled evaluation thresholds and the complete user journey were not executed in this review. Their acceptance evidence remains outstanding.
- No services were started, no browser automation was used, no credentials or AWS account were accessed, and no infrastructure was provisioned.

## Handoff to the implementation agent

1. Preserve Phase 1/2 compatibility, existing resource limits, immutable snapshots and the current protected-branch delivery process.
2. Correct the source locator and worker query; add a migrated-database regression that would catch the missing-column defect.
3. Complete isolated runtime composition and readiness gating using the pinned qualified toolchain.
4. Complete atomic report/graph/evidence publication through the existing persistence contract; verify terminal accounting and public event contracts against the live schema.
5. Fix request replay and verify authorized published-run reads, evidence scoping and complete UI/API flow.
6. Execute WP-3.13/3.14 gates from the committed plan, including compatibility, concurrency, process isolation, bounded capacity and labelled evaluation. Add Phase 3 CI coverage and record actual commands, commit, results and limitations.
7. Update acceptance and README only after those gates pass. Prepare Phase 4 against that verified commit; do not treat this review as permission to bypass any gate.

Phase 4 remains deterministic findings under the revised technical blueprint. Retrieval and agents belong to later phases. This review introduces no new architecture, schema changes or Phase 4 implementation decisions.

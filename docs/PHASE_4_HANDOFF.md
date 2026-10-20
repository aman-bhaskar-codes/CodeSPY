# Phase 4 handoff: deterministic findings

Phase 3 application repair commit: `27fbde44bfbdb493649e953cd9a4f84b68776eef`. Linux qualification passed in run `39360366079`; use the merged PR/main reference and acceptance record as the implementation baseline.

## Available inputs

- Immutable admitted snapshots and persisted file identities; original archive/manifest/policy integrity.
- Operation `analyze_structure`, profile `structural-small-v1`, immutable config/spec and fresh run per attempt.
- Published scoped graph/evidence and `ArchitectureReportV1`, exact source ranges/hashes and bounded verified excerpts.
- Python lexical candidates; JS/TS compiler candidates; import/candidate-call SCC and bounded traversal; FastAPI/Express/HTTP observations and explicit service-based candidate links.
- Runtime-gated API creation/retry; summary, section, graph, traversal, evidence and export contracts; generated TypeScript types and React report view.

## Rules for the next phase

Use the [technical blueprint](CodeSPY_Technical_Execution_Blueprint.md) phase order: Phase 4 is deterministic findings, Phase 5 retrieval, Phase 6 agents. Add findings under versioned rule/capability policies; observations and candidates must not become vulnerabilities merely because a cycle/import/route/reachability pattern exists. Do not infer complete CFG/PDG, taint or runtime behavior.

Respect tenant/repository/snapshot/run scope, current authorization, database time/fences, immutable history and atomic publication. Keep diagnostics source-safe. Missing dependencies, dynamic targets, partial source selection and unavailable graph sections remain visible abstentions. Never execute target scripts, install target packages or load target plugins.

The 100-MiB/10,000-entry admission limit differs from structural limits: 128 sources/16 MiB, 32 configs/1 MiB, 1,000 nodes/evidence, 2,000 edges/derivations and 2-MiB report. Larger scale needs an explicit versioned publication design. Coverage detail is bounded: omitted detail count plus listed omissions equals omitted files; inventory is authoritative.

Retry preserves the original spec principal and requires current write permission. Other writers create new analyses. Changed config/toolchain is a new spec. Do not reopen terminal runs or reuse stale worker authority.

## Verification and operations

Run the Phase 3 workflow and Phase 2 compatibility suite at the new head, plus appropriate new rule regressions. Preserve historical migrations and owner duplicate files. Evaluation provenance and synthetic limitations are in [acceptance](PHASE_3_ACCEPTANCE.md) and [manifest](../evals/phase3/manifest.json); promote held-out fixtures to development if used for tuning.

No Phase 4 code was added during this repair. AWS access, keys, browser automation, provisioning and deployment remain prohibited. Owned test resources must be cleaned; retain exact ownership records if cleanup fails. The laptop's Docker installation is unavailable, so Linux CI is the qualified container environment. Browser UX and live-provider/hosted checks remain separate.

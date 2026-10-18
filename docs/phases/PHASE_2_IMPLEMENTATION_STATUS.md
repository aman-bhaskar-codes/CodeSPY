# Phase 2 implementation status

Phase 2 is **in progress**. Phase 1 remains the accepted baseline at `fc39852b04909c04fbeb4c38a8b4c668a224f4d1`. The owner authorized plan revision followed by implementation on 18 October 2026. No AWS account, real service key, browser session or deployment was used.

## Planning deliverables

The complete [revised plan](PHASE_2_SAFE_INGESTION_IDENTITY_JOBS_EXECUTION_PLAN.md) incorporates timing, lifecycle, database-clock, persisted-file-ID and archive-identity decisions into the main specification. The [revision report](PHASE_2_PLAN_REVISION_REPORT.md) records the changes and resolution table. These are planning decisions, not evidence that all specified behavior is implemented.

## Implemented foundation

- Public Phase 1 persistence signatures remain intact; internal helpers share a caller-owned transaction and resolve stored file identities on replay.
- Revisions 0005–0008 add sixteen tables and nullable worker bindings. The Phase 1 test harness stays on revision 0004; the separate Phase 2 harness targets the current head on loopback port 55433.
- Database guards check organization, job, selected attempt, fence, lease and deadline for worker publication. They cover source insertion, snapshot sealing, bound runs/results and terminal successful job transitions. Jobs begin queued; run bindings cannot change.
- Pure ZIP inventory preflights ZIP32 metadata, validates paths/types/collisions, streams file hashes, classifies eligibility and separates archive digest from normalized file digest. No source is extracted or executed.
- Local storage uses an owned private directory, descriptor-relative no-follow access, atomic no-replace canonical creation, independent digest checks and exact descriptor deletion. Threaded I/O retains descriptor ownership through repeated cancellation.
- An aggregate monotonic budget includes all awaited forward work. Cleanup has a separate total bound with a synchronous transport fallback interface. These utilities are not yet connected to an application finalizer.

## Executed local evidence

| Check | Observed result |
|---|---|
| Legacy and initial Phase 2 unit/admission/storage suites | 58 passed |
| Phase 1 PostgreSQL integration suite at revision 0004 | 21 passed |
| Phase 2 PostgreSQL integration suite | 7 passed |
| Phase 2 migration/catalog suite | 2 passed |
| Strict application type checking | Passed, 41 modules |

The migration suite checks all 28 tables, API source-write restrictions, broker schema restrictions and a 0008 → 0004 → 0008 round trip retaining Phase 1 snapshot data. Integration checks cover concurrent replay with fresh candidate IDs, conflicting metadata, actual PostgreSQL lease expiry at inventory publication and snapshot sealing, cumulative individually short SQL exceeding a total budget, rollback/pool recovery, missing selected attempt, terminal insertion bypass and cross-attempt run binding.

The independent source review found three defects: cross-attempt run binding, terminal-job INSERT bypass and repeated cancellation during threaded storage I/O. Each was reproduced by a failing regression before correction. No review or test result establishes end-to-end Phase 2 acceptance.

## Remaining work and enablement gates

P2-00 qualification remains partial. P2-01 contracts/schema and P2-02 ports/harness are partial; P2-03 has the extraction, guards and initial regressions but no complete job service/finalizer. P2-06 has pure inventory only. Other work packages remain unimplemented.

Remaining work includes durable enqueue/claim/heartbeat/finalization and ambiguous COMMIT resolution; the complete terminal/retry/reservation accounting matrix; sessions, CSRF and repository/provider authorization; streamed upload/object-ledger lifecycle; S3/SQS emulator adapters and outbox dispatch; isolated Linux admission limits and the 50,000-file finalization benchmark; GitHub acquisition/revocation; recovery/cleanup/events; generated OpenAPI, API, UI and integrated acceptance.

The forced asyncpg transport-recovery path is only interface/mock tested; its live failure qualification remains required. The compression test establishes different ZIP digests with equal normalized manifests, but full acquisition/reuse/snapshot publication behavior still needs service-level tests. Inventory association eligibility conflicts and concurrent admission finalization are also pending.

No upload endpoint or worker entry point is enabled. Browser E2E, live OAuth/provider checks and hosted security acceptance were not executed. Phase 3 must not treat this branch as a completed Phase 2 handoff.

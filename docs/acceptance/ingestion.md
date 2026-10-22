# ingestion local acceptance

The local implementation covers safe ZIP ingestion, identity, durable jobs and inventory. The reference implementation was inspected at `fc39852b04909c04fbeb4c38a8b4c668a224f4d1`. persistence public persistence contracts remain available. This is local qualification; hosted approval and live provider qualification remain separate.

## Implemented boundaries

- Additive revisions 0005–0009: seventeen ingestion tables, including an immutable API command ledger for repository and binding replay. persistence stays at revision 0004 in its original harness.
- Opaque sessions, CSRF, encrypted OAuth grants, numeric GitHub identities, repository authorization, signed webhook revocation and bounded fixed-origin acquisition. Provider behavior is tested with fixtures; no real account or key was accessed.
- Bounded streaming uploads, tracked quarantine and canonical objects, immutable source manifests, persisted file-ID replay mapping and visible eligibility exclusions. ZIP contents are never executed or extracted into a shared source tree.
- SQS-compatible transport, transactional outbox, independent database recovery, global capacity two, attempt fences, cancellation, retry history and exactly-once reservation release.
- Finalization has one 60-second aggregate budget including locks and awaited COMMIT. PostgreSQL time determines terminal authority. Cleanup is separately bounded; ambiguous commit outcomes are reconciled against durable state.
- Typed scoped API pages and generated TypeScript contracts. The React interface supports upload, durable job progress, inventory child discovery, cancellation, retry, pagination and recoverable selection.

## Review resolutions

The independent review identified important defects. Each correction below has a regression in the delivered tests; wording alone was not used as verification.

| Finding                                          | Correction and regression                                                           |
| ------------------------------------------------ | ----------------------------------------------------------------------------------- |
| API role could forge worker settings             | Authority function execution restricted to broker; raw API-role publication test    |
| User authorization revocation omitted            | Numeric sender mapping and atomic grant revocation; signed delivery replay test     |
| S3 orphan lacked version descriptor              | Exact-key descriptor recovery and exact-version deletion; emulator orphan test      |
| Deleted canonical intent blocked fresh admission | Guarded new fenced intent reuses immutable byte identity; cleanup/re-admission test |
| Admitted quarantine retained indefinitely        | Expired staging reference released only with canonical proof; cleanup test          |
| Lease renewal stopped before storage checks      | Final pre-lock heartbeat after verification; slow verification regression           |
| UI followed admission only                       | Parent exposes inventory child; failed child UI regression                          |
| Rejected uploads could not reset                 | Explicit terminal reset; submission-only rejection UI test                          |
| Retry ambiguity generated new commands           | Session-scoped key retained across ambiguous responses; lost-response UI test       |
| Event cleanup could starve later histories       | Select only deletable histories; multi-batch retention regression                   |
| Runtime abandoned sibling consumer               | Own all in-flight tasks through shutdown; runtime test                              |
| API idempotency keys discarded                   | Atomic `api_commands` replay, input conflict and revoked-binding refusal tests      |
| Navigation and pagination absent                 | URL selection and scoped keyset pages; repository page test                         |

Additional regressions cover redirect-cookie stripping, transient metadata retention, recovery sweep progress and hard asyncpg transport invalidation. The original persistence demo exposed metadata contamination after in-process migration; public run projections now select persistence fields explicitly, and the original demo passes.

Canonical object rows retain byte identity when a deleted row begins a fresh intent; ownership/fence and version distinguish that intent. No historical submission, run or snapshot is reopened. Canonical available sources are not deleted by staging retention.

## Executed checks on 19 October 2026

| Check                                                                               | Result                                                                        |
| ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| ingestion database, API, migration, emulator, unit, storage and Linux admission suite | 92 passed                                                                     |
| Legacy and pure ingestion suites                                                      | 70 passed                                                                     |
| Original persistence integration / migration                                            | 21 / 3 passed                                                                 |
| Original synthetic persistence demo                                                     | Replay, rollback, scope and stale-write checks passed; owned database removed |
| Disposable ingestion CLI check                                                        | Passed; its database, queues and bucket removed                               |
| Frontend component tests                                                            | 5 passed                                                                      |
| Backend lint / strict types                                                         | Passed; 78 application modules                                                |
| Frontend lint / production build                                                    | Passed                                                                        |
| Static UI contract audit                                                            | Zero findings; does not establish browser behavior                            |
| 10,000-file capacity                                                                | Linux inventory 1.83 s; finalization 6.97 s; test passed in 10.53 s           |

The initial 50,000-file fixture exceeded the statement or total finalization budget. The advertised maximum is therefore **10,000 entries**, retaining the original timeout and lease safety contract. Measurements describe this local fixture, not a universal throughput guarantee.

## Deferred qualification

No AWS deployment, IAM/network validation, real OAuth/App registration, provider secret, browser automation or live provider integration was performed. Browser accessibility, responsive interaction and hostile hosted multi-tenant isolation require their later authorized gates. S3/SQS evidence comes from the pinned local emulator, not AWS. Inventory is source metadata, not structural analysis or a security finding.

structural analysis consumes persisted snapshot/file IDs, archive and manifest digests, policy/exclusion metadata and fenced operation contracts. It must preserve these lifecycle and identity rules. GitHub CI publication results are recorded on the delivery pull request.

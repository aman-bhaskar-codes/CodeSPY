# structural analysis acceptance

Date: 20 October 2026. Repair baseline: `e98554509faeb8ed74d8c40bcb3fc1eec8ece5c4`. Latest application repair: `27fbde44bfbdb493649e953cd9a4f84b68776eef`.

**Qualified for the bounded structural profile below.** The final application repair passed Linux qualification. Normal protected PR/main checks remain the delivery gate; hosted and browser acceptance are separate.

## Delivered behavior

- The broker resolves original verified archive/version, manifest, admission policy, language and persisted file identities. No guessed manifest or default-policy fallback.
- The pinned offline Linux analyzer receives generated read-only inputs with no network, host project/socket or inherited service credentials. Readiness checks image identity and a trusted Python/JS/JSX/TS/TSX fixture. Parser operations have external deadlines; no unsafe Tree-sitter progress callback.
- Draft validation checks source bytes/ranges, identities and all graph/report references. One fenced transaction publishes report, graph, evidence, run/job terminal state and event. Ambiguous COMMIT recovery checks durable report and batch hashes.
- Immutable specs and principal-bound request replay/retries preserve history. Retry requires the original principal's current write grant. Another writer starts a new analysis/spec.
- APIs expose bounded summaries, paged sections/graph, bounded traversal, scoped evidence and exports. React renders source as escaped excerpts, reports partial coverage, and supports cancel/retry. Creation and retry are runtime gated.
- All historical migrations remain unchanged; structural head remains `0011_structural_authority`, with 31 tables. persistence public persistence and ingestion V2 contracts remain in place.

## Verification evidence

| Environment/check                          | Observed result                                                                                                                                                                |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Local service-free persistence, ingestion and structural analysis suites      | 268 passed, 3 container-dependent checks skipped                                                                                                                               |
| Ruff / strict mypy                         | Clean; 114 application modules                                                                                                                                                 |
| TypeScript analyzer                        | 11 passed                                                                                                                                                                      |
| Frontend                                   | 8 passed; typecheck, lint and production build passed                                                                                                                          |
| OpenAPI / TypeScript generation            | Regenerated; Linux CI confirmed no drift                                                                                                                                       |
| Latest PostgreSQL 16 / Linux container run | 225 structural analysis passed; ingestion at structural head: 44 passed, 1 skipped; cleanup succeeded. [39360366079](https://github.com/aman-bhaskar-codes/CodeSPY/actions/runs/39360366079) |

Regressions cover the original integration failures and six final-review findings: annotation/class-keyword scope traversal, dotted-import identity, independent source/config budgets, dense report byte bounds, supervised pipe readers, and durable exact-owner cleanup. Additional checks cover principal ownership, queued deadline recovery, database-time expiry at the write guard, candidate-call SCC projection and package metadata separation.

## Maximum-profile measurements

Both fixtures selected 128 sources with 16,777,216 original source bytes and completed two fresh runs with identical normalized results. TypeScript additionally admitted 32 independent project configs. Measurements describe these synthetic fixtures, not a latency guarantee.

| Language   | Retained symbols | Two-run elapsed | Analyzer parent RSS | Container cgroup peak |
| ---------- | ---------------- | --------------- | ------------------- | --------------------- |
| Python     | 384              | 110.549 s       | 66,568 KiB          | 73,814,016 bytes      |
| TypeScript | 512              | 121.292 s       | 190,692 KiB         | 277,774,336 bytes     |

Qualified Linux image: `sha256:818d737dde5ff0da44c09c1d1790ec45a0af9ecf21b969070bb87c6a8e4b5eeb`. Trusted build-context/toolchain digest: `bc6d09a743a079e337e5900c563336c5f01ac6c33d4a7fd468591bc6256a62ff`. Future image/platform changes require fresh qualification. PostgreSQL and LocalStack test containers were removed by the successful cleanup step. The single ingestion compatibility skip is the admission-container test, which requires its separate image; the ingestion foundation workflow builds and tests that image.

## Evaluation provenance

[Fixture manifest](../../evals/structural analysis/manifest.json) records source hashes, lineage and splits. `development-v1` was promoted after an overload-mapping failure; it is not held out. `heldout-v2` has 80 static call sites per Python and JS/TS group: 30 positive, 40 negative and 10 ambiguous. Each group produced TP30/FP0/FN0, all 10 ambiguous labels correct and zero falsely unique negative calls. Each FastAPI, Express and HTTP-link synthetic task produced TP20/FP0/FN0 from 20 positive and 20 negative cases.

These are agent-authored labels and repeated synthetic template variants. They establish narrow regression thresholds, not independent human evaluation, runtime ground truth, general repository accuracy or security correctness.

## Limits and unexecuted checks

The [capability record](../capabilities/structural-small-v1.md) defines resource limits and measured profiles. Browser/manual UX acceptance, live GitHub OAuth/App behavior, hosted AWS isolation/privacy/retention and real-provider integration remain unexecuted. No AWS account, real keys, browser automation or deployment was used. Local Docker cannot launch; no local test services were started.

Consumers must preserve partial/ambiguous/unavailable coverage. There is no CFG/PDG, taint, runtime, vulnerability, retrieval or agent claim.

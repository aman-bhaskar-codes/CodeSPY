# Phase 4 — deterministic findings execution

Controlling roadmap: [post-Phase-3 roadmap](../CODESPY_POST_PHASE3_MASTER_EXECUTION_ROADMAP.md). Inspected baseline: `b13a4f5dcc961d67b53bb381801eed3f54f1cdec`. This is a specification, not acceptance evidence.

## P4.00: rebase and preserve

Reuse `analysis_sources.locate_source/stage_source`, the injected AnalysisRunner, structural result materialization and fenced finalizer. Preserve migrations 0001–0011, source hashes, persisted file IDs, principal-bound retries and aggregate finalization budget. No duplicated source-attestation authority. Existing Phase 3 tests are compatibility gates. AWS, paid providers, credentials and deployment remain inactive.

The earlier detailed Phase 4 plan referenced by the roadmap is not present in the supplied checkout or attached local paths. This specification records that gap rather than claiming it was inspected. Subsystem packages receive exact migration/interface specifications before their implementation.

## P4.01: scanner boundary (first increment)

Introduce strict frozen scanner/profile/request/result contracts. Profiles identify a fixed tool, artifact digest, rules digest and qualified resource limits; no executable path, arbitrary argv, environment or URL accepted. Requests bind repository, persisted run scope, immutable configuration, selected file IDs and authenticated hashes. Worker-local aliases are generated from file IDs, never target paths.

Results echo a canonical digest of the entire request, including run/repository/configuration/profile and selected sources; this is correlation, not proof of execution. Results contain only known rule IDs, file IDs, source hashes and original ranges. No arbitrary messages, matches, secret values, snippets or value-derived fingerprints. Completed, partial, failed, unavailable and cancelled outcomes remain distinguishable. Coverage explicitly accounts for selected, processed and omitted files. Non-success outcomes cannot claim an empty successful scan. Unknown rules, duplicate observations, foreign files/hashes or out-of-file ranges fail validation before publication. Contract validation is not source attestation; the trusted broker must validate range/hash against actual staged bytes.

First proof: malformed/extra fields, unknown rule, duplicate IDs, mismatched source hash, count/byte limits, failed-result coverage and Unicode safe errors. Run focused tests, Ruff and strict typing, then historical Phase 3 unit suite. No scanners enabled by these DTOs.

Admission metadata V2 is a separate increment: inspect archive classification, manifests, acquisition/replay and DB policy constraints before changing them. Preserve V1 byte identity and replay; exact uv.lock opt-in applies only to future admissions. Old exclusions remain unchanged.

## Dependent packages

- P4.02: pin and qualify Semgrep CE/Gitleaks offline images; authored/attributed rules; fixed argv and private capped pipes; normalize only safe metadata. Required positive/negative, Unicode/CRLF, malformed/flood/hang/cancel/cleanup and secret-canary tests. Release and license checks precede download.
- P4.03: qualified package-lock/uv.lock parser and frozen offline OSV bundles. Missing, explicit empty, withdrawn, stale and unsupported remain different. No target installs or hidden network fallback.
- P4.04: additive scoped scan/observation/finding/verification tables and authoritative fenced publication. Exact FKs/roles and complete-set checks specified against then-current schema. Real PostgreSQL cancellation, lease expiry, ambiguous COMMIT and atomicity qualification required.
- P4.05: append-only versioned triage with conflict 409; bounded generated API/UI and native two-hop traces. No automatic fixed state; zero-findings display includes tool coverage.
- P4.A: optional pinned REA static MCP qualification only after native traces; package provenance, schema/resource/privacy and differential usefulness gates. Not an authorization/evidence authority.

## Acceptance and delivery

A usable upload → structural report → cited deterministic finding → triage flow works without model keys. Advertised categories require at least 20 positive and 20 negative lineage-labelled cases and frozen evaluation; proposed precision ≥0.95 and recall ≥0.80 are unexecuted targets. Zero secret-sink, scope, range and fence violations in required fixtures. Linux owned-container CI qualifies actual tools; unavailable laptop Docker is not a passed check. Record exact commits, commands, results, skips and limitations. Feature flags stay off until capability qualification. Rollback disables new scan admission while preserving immutable history. Normal PR checks precede any requested merge.

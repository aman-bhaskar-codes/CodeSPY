# Findings materialization

> **Status reconciliation — 28 October 2026:** Read this component record with [durable findings](durable-findings.md), [findings/retrieval checks](retrieval-boundaries.md) and [workspace verification](frontend-workspace.md). Later scoped APIs and UI exist; this materializer record alone is not their qualification. No tests were rerun for this documentation update.

The broker can pass verified offline scanner and dependency results to a pure typed materializer. This does not enable public scanner jobs, API routes or triage. Durable storage integration is recorded separately in [durable findings](durable-findings.md).

## Contracts and integration

`ScannerPort.scan` and dependency `assess` retain their result contracts. Internal `scan_attested` and `assess_attested` methods return broker execution receipts after existing containment and source checks finish. Native receipts follow the independent range-verification container. A receipt has no wire decoder and is an internal composition marker, not cryptographic authority against arbitrary Python code inside the trusted broker process.

The publication specification binds organization, repository, snapshot, run, configuration, runtime image, reviewed catalog, selected requests and an explicit aware capture/evaluation time. Results must match the complete requested tool set. Offline advisory assessments are recomputed against the exact frozen bundle; missing, empty, stale, withdrawn and incomplete answers remain different.

Occurrences preserve source file/hash/range or exact lockfile/package location/query/advisory-record digest. Reviewed policies supply public wording and recommendations. Evidence uses the existing envelope and scoped references. Static predicate support does not prove exploitation, execution, credential validity, installed dependency state or reachability. Runtime verification is not available. Historical draft consistency does not establish current support availability or authorization; those remain requirements for the later persistence/read boundary.

Bounds remain two native results of at most 1,000 observations each, a dependency inventory of at most 5,000 instances, 1,000 selected finding occurrences and a 4 MiB aggregate draft. Overflow of the finding count has deterministic selection and explicit omissions/partial coverage; byte overflow rejects the draft rather than dropping support. Raw scanner text, secret values, value-derived secret fingerprints, advisory prose and environment-marker expressions are not retained. Whole-file hashes remain existing source-integrity metadata.

## Observed checks

On 21 October 2026, Python 3.12.12:

- 30 focused findings/receipt tests passed in 1.52 seconds, including scope/runtime/catalog/version mismatches, missing results, source-evidence correlation, empty obligations, unsupported runtime claims, secret canaries, exact advisory status handling, instance preservation, omissions and byte overflow.
- The selected supported regression command passed 509 tests with 65 opt-in skips in 7.46 seconds.
- Strict mypy checked 139 application source files; Ruff and whitespace checks passed.

Commands and full local outputs are retained in ignored verification records. The all-directory historical command initially exposed a missing Alembic in the temporary environment and colliding test basenames. Installing the already-locked Alembic 1.20.0 into that temporary environment and using `--import-mode=importlib` allowed collection. Execution then stopped at `tests/integration/test_demo.py::test_demo_assertions` because no owned PostgreSQL fixture was configured (the loopback-port guard rejected the empty configuration). The complete historical database suite has not passed in this verification. No migrations were changed for findings materialization.

Real Linux qualification passed 20 tests in 235.52 seconds using immutable scanner image `sha256:ddc0aab8d60862f81fd1cfc6f43ce55e618497080f160fb56abe7e11c2ade6c2` with the current host adapters and materializer. The worker entrypoint, normalizers and parser closure were unchanged, so this run reused the previously built image; it is not a claim that a new all-source image was built. The run covers ten offline dependency cases, both native scanners' smoke/format/128-file/16-MiB paths, runtime identity, crash cleanup, output overflow and containment controls. Native results were materialized into scoped findings, evidence and decisions, with expected counts and secret-canary checks. The two maximum-size test calls took 40.49 seconds for Semgrep and 94.44 seconds for Gitleaks, including setup and independent range verification; these are fixture timings, not production latency guarantees. Existing execution/memory limits remain unchanged; peak memory was not measured.

Twenty generic opt-in skips were separately exercised in this Linux run. The remaining 45 opt-in cases (40 standalone native-format cases, three admission/policy-image cases and two database compatibility cases) were not executed in this verification. Linux format corpora cover the selected native formats, but do not turn the standalone skipped cases into passes. Unit fixtures using internal receipt issuance remain synthetic trusted-broker seams.

A fresh read-only engineering review found no blocking issues. Deferred nonblocking coverage: one combined publication fixture containing both native tools and dependency assessment; individual paths are covered. Database atomicity and read-time authorization/support availability were outside this materializer increment; see the separate durable findings qualification. Public API readiness remains outside these internal capabilities. Owned test containers were removed; no application or cloud service was started.

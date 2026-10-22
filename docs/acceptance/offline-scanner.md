# Offline scanner integration evidence

The scanner adapter is development-only. No public scan endpoint, durable scan-job publication, findings API or vulnerability-free claim is enabled by this record.

## Implementation under test

This record describes uncommitted working-tree changes based on `e754f550fbb61b54f646c1a7df120f26309a5c08`. It is local verification evidence, not evidence for a published GitHub commit or CI run.

The immutable Linux image under final verification is `sha256:5aafbf866fc724868fd6af94ba3c297713fbf2584849104aff92b0188991db11`. Its runtime manifest authenticates Semgrep CE 1.179.0, Gitleaks 8.30.1 and authored rule digests. Build inputs are restricted to trusted application Python files and the explicit scanner build files. No environment files, credentials or uploaded repositories enter the build context.

Subsequent dependency integration and cancellation fixes were qualified on image `sha256:ddc0aab8d60862f81fd1cfc6f43ce55e618497080f160fb56abe7e11c2ade6c2`, including the same scanner maximum-profile and recovery tests. See the [current dependency integration record](offline-dependencies.md) for the newer results; the table below records the earlier scanner increment.

The path under test is LocalObjectStore -> authenticated admitted archive/manifest -> persisted selected file IDs -> isolated native scanner -> metadata-only result -> fresh isolated original-byte verifier -> exact result-digest acceptance. The verifier invokes no native scanner. Both containers share the original 180-second aggregate execution budget and have separate durable ownership records.

## Observed checks

| Check | Observed result |
|---|---|
| Supported Python 3.12.12 local regression selection | 450 passed, 55 opt-in checks skipped, 7.55 seconds |
| Strict mypy without incremental cache | No issues in 132 application source files |
| Pinned Ruff | All checks passed |
| TypeScript analyzer on Node 22.23.3 / TypeScript 5.9.3 | 11 tests passed; strict typing passed |
| Frontend on Node 22.23.3 | 8 tests passed; typing, lint and production build passed |
| Reviewed-image Linux integration | 10 passed in 226.24 seconds, including both maximum-profile runs and owned recovery |

The Python selection includes shared source reconstruction, exact request identity, runtime binding, advisory/dependency contracts, native report normalization, process output/deadline/cancellation guards, source staging and ownership recovery. Mocked controller tests establish contracts only. The skipped checks require separate owned database/admission images or explicit native opt-in. They are not passing checks.

Frontend checks used an owned source/lock copy and `npm ci --ignore-scripts --no-audit --no-fund`, preserving the existing shared install, whose Vitest package was invalid. Existing dependency deprecation warnings are recorded locally; no dependency upgrade or lock change was made to hide them. Python checks used a temporary supported runtime with the frozen workspace pins after the original local installation stalled during file reads. Python 3.13 diagnostic runs are excluded from acceptance.

## Native fixtures and boundaries

The native suite covers positive/negative synthetic source fixtures, original stored file identities, selected secret-format Unicode/CRLF/final-line cases, 128 source files totaling exactly 16 MiB per tool, readiness/version authentication, output overflow and durable exact-owner recovery. The large fixtures place positive evidence near file ends. Synthetic variants exercise these selected predicates; they do not establish broad vulnerability accuracy or real credential validity.

Each container requests and verifies no network, no persistent log driver, readonly root and three readonly input mounts, UID/GID 65534, dropped capabilities, no-new-privileges, 1 GiB memory/swap, two CPUs, 64 PIDs, private 64-MiB tmpfs and explicit ulimits. Exact peak memory has not been measured. Enforced limits must not be described as a peak-memory measurement.

A fresh review found missing independent coordinate verification, lost native timeout identity and incomplete actual tmpfs/ulimit checks. Regression tests reproduced those gaps before fixes. The current implementation adds independent isolated range verification, a safe timeout exit status and actual control checks. The reviewed-image native suite passed after those fixes. The complete 128-file/16-MiB test calls took 41.84 seconds for Semgrep and 97.57 seconds for Gitleaks, including fixture setup and independent verification; these are observed test durations on this machine, not latency guarantees. A final exact-name Docker listing confirmed no scanner-owned test containers remained.

The declared MCP 1.29.0 transitive dependency has a known advisory affecting stateful HTTP-server operation. The fixed CLI image does not enable an MCP server or network access. This is a constrained exception, not a clean dependency-audit claim. See the [scanner image contract](../../infra/scanner/README.md) and [technical qualification notes](../engineering/scanner-qualification.md).

## Remaining qualification

Durable scan-job/database publication, application API/UI integration, independently curated detector evaluation, precise peak-resource measurement, broader crash-window testing and provider-backed execution require their own implementation and evidence. Existing fixture/provider mocks are not live-service qualification. No AWS access, credentials, paid calls, deployment or GitHub writes occurred during these checks.

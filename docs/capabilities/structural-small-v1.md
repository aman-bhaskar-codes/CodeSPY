# structural-small-v1 capability record

Date: 19 October 2026. Package: WP-3.00. Baseline: `736a5ef831ed4a81899916153a7d09a13a51f2d1`. Feature branch: `feat/phase3-toolchain`.

**Product readiness: disabled.** Tiny trusted toolchain fixtures qualify; structural source access, production isolation, analyzers, report publication, API and UI remain later packages. This is not a Phase 3 acceptance claim.

## Observed environment and exact pins

- Development host: macOS/Darwin arm64, 8 GiB RAM. Python 3.12.12; uv 0.10.12; Node 22.23.3; npm 10.9.9. Docker Desktop was initially off, started only for owned qualification, and must be stopped at handoff if no other containers exist.
- Docker client 29.0.1; Compose v2.40.3-desktop.1. Qualification used native Linux arm64 containers. Other worker architectures remain unqualified.
- Tree-sitter binding 0.26.0; direct Python grammar 0.25.0; direct JavaScript grammar 0.25.0; TypeScript/TSX grammar 0.23.2. Runtime ABIs: Python/JS 15, TypeScript/TSX 14; both admitted by the installed binding.
- TypeScript 5.9.3 and @types/node 22.20.5 are pinned independently under `tools/typescript-analyzer/package-lock.json`. No frontend lock was changed.
- Root `uv.lock` remains authoritative. Only three grammar packages were added; no existing resolved package version changed. Build installs accept locked binary wheel hashes only. Language-pack 1.22.1 remains dormant.
- Licenses: Tree-sitter and the three direct grammars MIT; TypeScript Apache-2.0; Node carries its own MIT/third-party notices. The qualification image retains Node and libstdc++ notices. REA 6.7.0 is an engineering tool, not a product dependency.

## Executed qualification

| Check                                                                            | Observed result                                                                                                                           |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Existing pure baseline                                                           | 59 passed before dependency changes                                                                                                       |
| Parser pin/ABI/query/range/error/missing-token tests                             | 10 passed                                                                                                                                 |
| Control ownership, image validation, stdout/stderr bounds and child cancellation | 6 passed                                                                                                                                  |
| TypeScript exact version, TSX UTF16/UTF8 distinction, virtual-host local binding | 3 passed                                                                                                                                  |
| Offline Linux Python and Node/compiler round trip                                | Passed in 1.937 s                                                                                                                         |
| Python probe peak RSS                                                            | 24476 KiB; Python process only, not whole-process-tree maximal RSS                                                                        |
| Native progress callback                                                         | Correct two-argument callback crashed a child with SIGSEGV on both host and Linux; disabled                                               |
| Offline runtime profile                                                          | Nonroot uid65534, network none, read-only root, drop all capabilities, no-new-privileges, 1 GiB memory/swap cap, PID64, CPU2, tmpfs64 MiB |
| Cgroup observations inside the container                                         | memory.max=1073741824, pids.max=64                                                                                                        |
| Deliberate output flood                                                          | Stopped at 65,536-byte stream bound while draining                                                                                        |
| Cancel after observed child startup                                              | Container/control cleanup completed in 0.177 s                                                                                            |
| Probe cleanup                                                                    | Zero owned containers remaining                                                                                                           |

Measured times describe these tiny trusted fixtures. Full memory/PID exhaustion, all descendants under repeated cancellation, interrupted create reconciliation, hostile parser inputs and complete structural resource capacity remain WP-3.02/13 requirements. Cgroup values and one cancellation proof do not establish hostile multi-tenant isolation.

The pinned Linux qualification image is `sha256:8aff85c8f00210447e7f041a88feac8717c035a964e124454f38b154982a7a71`. Python base: `python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c`. Node build stage: `node:22.23.3-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392`. Local image IDs identify this build; future builds need fresh IDs and qualification.

The combined existing and new pure suite passed 77 tests; strict application typing passed 78 modules. Lock consistency and environment dependency checks passed. The final review found a full-pipe cleanup deadlock; 10-MB stdout/stderr watchdog regressions failed before the fix and passed after drain/discard termination was implemented.

## Supported parser use and explicit fallback

The tested safe route passes bounded original bytes to `Parser.parse`. Do not set removed `timeout_micros`, rely on `progress_callback`, or mistake Python counters for native memory limits. Use external per-operation deadlines and owned process-tree cancellation in the future production runner. The callback diagnostic runs in a separate trusted subprocess so an upstream fault cannot crash the qualification controller. No upstream source patch or broad dependency upgrade was made.

## Reproduce

Use the documented Node22 runtime. These commands install only trusted pinned development dependencies and run synthetic fixtures, never target repository scripts:

```bash
uv sync --all-packages --all-groups --frozen
npm --prefix tools/typescript-analyzer ci --ignore-scripts
uv run --all-packages --all-groups pytest -c backend/pyproject.toml backend/tests/phase3/unit
npm --prefix tools/typescript-analyzer run test:capabilities
uv run --all-packages --all-groups python scripts/build_analysis_qualification_image.py --image-out work/phase3-probe-image.id
uv run --all-packages --all-groups python scripts/qualify_structural_toolchain.py --image-file work/phase3-probe-image.id --output work/phase3-probe-result.json
```

Choose fresh output filenames; the build/qualification scripts refuse existing output files. The build context is a generated whitelist containing parser hashes, trusted fixture code and the compiler lock. It excludes developer .env, source repositories, owner staging and node_modules. Runtime gets no network, host project bind mount, Docker socket or cloud/database/model credentials. Pending cleanup retains an exact marker/name record under the ignored output directory; never delete containers by prefix.

## Remaining gates

WP-3.01 contracts/schema; WP-3.02 production runner and full adversarial resource checks; WP-3.03 authenticated source bytes and position mapping; subsequent syntax/binding/graph/framework/report/jobs/API/UI/evaluation packages. Structural 128-file/16-MiB/1,000-node budgets remain proposed until measured at release qualification. No LLM, scanner, AWS, live OAuth, browser automation or deployed service was involved.

See [ADR 0003](../adr/0003-structural-analysis.md), the [execution plan](../phases/PHASE_3_STRUCTURAL_ANALYSIS_EXECUTION_PLAN.md) and [REA evidence](../engineering/phase3-rea-investigation.md).

# structural-small-v1 capability record

Updated 20 October 2026. Application repair `27fbde44bfbdb493649e953cd9a4f84b68776eef`; Linux qualification passed. Release evidence belongs to [structural analysis acceptance](../acceptance/structural-analysis.md). The initial toolchain qualification probe below is historical, not current integrated acceptance.

## Implemented profile

Python and JS/JSX/TS/TSX sources; Tree-sitter 0.26.0, Python grammar 0.25.0, JavaScript grammar 0.25.0, TypeScript/TSX grammar 0.23.2, TypeScript compiler 5.9.3. Target packages, scripts, plugins and runtime execution are excluded. Trusted dependencies are pinned and built offline into the worker image.

| Boundary                | Limit                                                                               |
| ----------------------- | ----------------------------------------------------------------------------------- |
| Admission               | 100 MiB compressed; 10,000 entries; original expansion/exclusion rules              |
| Structural source       | 128 eligible files; 16 MiB total; 2 MiB/file                                        |
| Config                  | 32 files; 1 MiB aggregate; 256 KiB/file                                             |
| Compiler JSON transport | 20 MiB after escaping; over-cap resolution becomes unavailable/partial              |
| Published result        | 1,000 nodes/evidence; 2,000 edges/derivations; 8 MiB batch; 2 MiB report            |
| Runtime                 | 1 GiB memory/swap; 2 CPU; 64 PIDs; Node old-space 256 MiB                           |
| Deadline                | 180 s aggregate; parser/checker operation at most 20 s; aggregate finalization 60 s |
| Diagnostics/output      | 64 KiB stdout/stderr; 16 MiB draft file                                             |
| Reads                   | 64 KiB report summary; section/graph pages ≤100 records and ≤256 KiB                |

Report byte shaping includes actual map/excerpt overhead and whole-file omissions. Exact omission counters remain available even when per-file detail is bounded. Complete byte inventory never means complete semantic analysis. The profile is local/CI-qualified only; native parser containment and synthetic workloads do not establish hosted multi-tenant isolation.

## Current qualification

Local service-free suites: 268 passed, 3 Linux-container checks skipped. TypeScript suite: 11 passed. Frontend: 8 passed plus type/lint/build. Ruff and strict typing clean. Linux CI [39360366079](https://github.com/aman-bhaskar-codes/CodeSPY/actions/runs/39360366079) passed 225 structural analysis tests, including five-dialect smoke, PostgreSQL publication/recovery and both maximal source cases. ingestion at structural head passed 44 tests, with its separate admission-image check skipped. Cleanup passed.

Current measured fixtures: Python 128 files/16 MiB/384 symbols, two normalized-equal runs in 110.549 s, parent RSS 66,568 KiB, cgroup peak 73,814,016 bytes. TypeScript 128 files/16 MiB plus 32 configs/512 symbols, two normalized-equal runs in 121.292 s, parent RSS 190,692 KiB, cgroup peak 277,774,336 bytes. These are synthetic measurements under existing limits, not general performance guarantees. Qualified Linux image and toolchain digest are recorded in acceptance.

For historical comparison only, the prior `632a1ba` CI run passed 203 structural analysis tests and 44 ingestion integrations (one skipped). Its Python maximal fixture contained 128 files/16 MiB/384 symbols, with two normalized-equal runs in 152.704 s and cgroup peak 74,858,496 bytes. These figures predate the final review fixes and do not qualify the new TypeScript/config case or latest image.

Local Docker is unavailable because its installed app lacks its executable. No local test service was started during repair. Linux CI is the container qualification environment; future platforms/images need their own results. Exact toolchain context identity is generated from the trusted build whitelist, not guessed or all-zero.

## Historical toolchain qualification evidence

The following record describes tiny arm64 probes on 19 October 2026 only. It predates production integration and does not establish current release readiness.

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

Measured times describe these tiny trusted fixtures. Full memory/PID exhaustion, all descendants under repeated cancellation, interrupted create reconciliation, hostile parser inputs and complete structural resource capacity remain toolchain qualification requirements. Cgroup values and one cancellation proof do not establish hostile multi-tenant isolation.

The pinned Linux qualification image is `sha256:8aff85c8f00210447e7f041a88feac8717c035a964e124454f38b154982a7a71`. Python base: `python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c`. Node build stage: `node:22.23.3-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392`. Local image IDs identify this build; future builds need fresh IDs and qualification.

The combined existing and new pure suite passed 77 tests; strict application typing passed 78 modules. Lock consistency and environment dependency checks passed. The final review found a full-pipe cleanup deadlock; 10-MB stdout/stderr watchdog regressions failed before the fix and passed after drain/discard termination was implemented.

## Supported parser use and explicit fallback

The tested safe route passes bounded original bytes to `Parser.parse`. Do not set removed `timeout_micros`, rely on `progress_callback`, or mistake Python counters for native memory limits. Use external per-operation deadlines and owned process-tree cancellation in the future production runner. The callback diagnostic runs in a separate trusted subprocess so an upstream fault cannot crash the qualification controller. No upstream source patch or broad dependency upgrade was made.

## Reproduce

Use the documented Node22 runtime. These commands install only trusted pinned development dependencies and run synthetic fixtures, never target repository scripts:

```bash
uv sync --all-packages --all-groups --frozen
npm --prefix tools/typescript-analyzer ci --ignore-scripts
uv run --all-packages --all-groups pytest -c backend/pyproject.toml backend/tests/structural analysis/unit
npm --prefix tools/typescript-analyzer run test:capabilities
uv run --all-packages --all-groups python scripts/build_analysis_qualification_image.py --image-out work/structural analysis-probe-image.id
uv run --all-packages --all-groups python scripts/qualify_structural_toolchain.py --image-file work/structural analysis-probe-image.id --output work/structural analysis-probe-result.json
```

Choose fresh output filenames; the build/qualification scripts refuse existing output files. The build context is a generated whitelist containing parser hashes, trusted fixture code and the compiler lock. It excludes developer .env, source repositories, owner staging and node_modules. Runtime gets no network, host project bind mount, Docker socket or cloud/database/model credentials. Pending cleanup retains an exact marker/name record under the ignored output directory; never delete containers by prefix.

## Gates that remained after the historical probe

toolchain qualification contracts/schema; toolchain qualification production runner and full adversarial resource checks; toolchain qualification authenticated source bytes and position mapping; subsequent syntax/binding/graph/framework/report/jobs/API/UI/evaluation packages. Structural 128-file/16-MiB/1,000-node budgets remain proposed until measured at release qualification. No LLM, scanner, AWS, live OAuth, browser automation or deployed service was involved.

See [ADR 0003](../adr/0003-structural-analysis.md) and [REA evidence](../engineering/rea-investigation.md).

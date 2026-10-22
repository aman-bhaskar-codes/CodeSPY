# Offline dependency assessment evidence

This adapter assesses selected admitted lock metadata against an injected frozen advisory bundle. It does not install packages, contact providers, establish runtime applicability or publish findings through the application API.

## Implementation and identity

The implementation is an uncommitted working-tree change based on `e754f550fbb61b54f646c1a7df120f26309a5c08`. The locally qualified Linux image is `sha256:ddc0aab8d60862f81fd1cfc6f43ce55e618497080f160fb56abe7e11c2ade6c2`. This is local evidence, not CI qualification of a published commit.

The typed port binds repository/run scope, selected persisted file IDs/hashes, configuration, parser profile, immutable runtime digest, bundle digest and evaluation time. The object-store stager reuses admitted-object streaming. The worker authenticates the original archive and manifest under the persisted admission policy, then parses eligible `package-lock.json` v3 or `uv.lock` v1/revision3 metadata inside the existing isolated container. It invokes no native scanner or target package manager in this mode.

Each selected package instance retains its lock location. Nested npm versions, npm aliases and uv alternatives remain separate even when exact advisory queries are reused. Unsupported/private identities remain unresolved. uv entries excluded under the earlier admission policy remain excluded; this adapter cannot promote them.

Marker expressions stay inside the private parser. Results retain instance locations, marker counts and unresolved runtime applicability. Full advisory documents, raw marker expressions, arbitrary messages and private registry URLs are not forwarded into the metadata result. A matched advisory is evidence of the frozen query answer, never proof of reachability or an installed runtime vulnerability.

## Coverage and bounds

Explicit empty, missing, stale, incomplete, withdrawn and invalid answers remain distinct. Complete coverage means the selected lock inventories and supported package identities were assessed, not that the entire repository or runtime is free of vulnerabilities. No selected metadata yields unknown coverage. Invalid/excluded metadata cannot produce a complete empty scan.

The request permits at most 32 metadata files, 256 KiB each and 2 MiB aggregate. The result permits at most 5,000 package instances and 2 MiB serialized metadata. Capacity failure is explicit; the implementation does not silently truncate into clean absence. The worker reuses the existing 180-second deadline and verified networkless/nonroot/readonly container controls, durable ownership and bounded cleanup. Exact peak memory is not measured.

The existing MCP transitive dependency advisory exception in the native image remains documented in the [scanner image contract](../../infra/scanner/README.md). No MCP server is enabled by this dependency assessment adapter.

## Current checks

| Check | Observed result |
|---|---|
| Supported Python 3.12.12 regression selection | 479 passed; 65 opt-in cases skipped in this selection; 11.67 seconds |
| Focused process/ownership cancellation checks | 19 passed in 1.22 seconds |
| Strict mypy without incremental cache | No issues in 137 application source files |
| Pinned Ruff | All checks passed |
| TypeScript analyzer on Node 22.23.3 | 11 tests passed; typing passed |
| Frontend on Node 22.23.3 | 8 tests passed; typing, lint and production build passed in the owned source/lock copy |
| Current-image Linux qualification | 20 passed in 238.04 seconds: 10 dependency cases and 10 scanner/control/limit checks |

The generic Python selection skips database/admission-image checks and explicit native opt-ins. Twenty of its opt-in cases were separately executed successfully in the current-image native suite; the other 45 checks were not executed in this verification. Mock controller tests are labelled as contract checks and do not substitute for that suite. Frozen advisory fixtures are synthetic; they are not live-provider qualification. The frontend verification copy matched all 41 checked current source/config files, excluding environment files and generated output.

Fresh review identified collapsing unresolved uv instances and cancellation being replaced by cleanup failure. Regressions reproduced both, including a bounded cleanup timeout. Additional regressions reproduced malformed SemVer identities, missing uv inventory and the same cancellation failure at the owned subprocess reaper. The supported and native checks above cover the fixes. Maximum scanner test calls, including fixture setup and independent range verification, took 39.13 seconds for Semgrep and 95.34 seconds for Gitleaks. These are observed test durations, not latency guarantees. A final exact-name Docker listing confirmed no owned scanner test containers remained.

## Research and remaining integration

The selected design reuses CodeSPY's bounded parsers and frozen exact-query evidence. Alternatives researched were [OSV-Scanner/SCALIBR](https://github.com/google/osv-scanner) and [Grype](https://github.com/anchore/grype); neither was added as a dependency. OSV-Scanner source/tests distinguish offline vulnerability data from disabling all network-capable plugins. The [OSV batch contract](https://google.github.io/osv.dev/post-v1-querybatch/) requires per-query pagination handling; an incomplete answer cannot support absence. Version grammar follows [SemVer 2.0](https://semver.org/spec/v2.0.0.html); no ecosystem range comparison or lexicographic version ordering is introduced.

Durable scan jobs, scoped findings/evidence tables, fenced atomic publication, application APIs/UI, triage and end-to-end evaluation remain separate work. No AWS account access, keys, paid calls, provider requests, deployment or GitHub writes occurred.

# Source reader acceptance

Baseline: `83767b105f0eb0870c79f84402241d729084120b` plus local uncommitted
source-reader changes. This is a bounded qualification, not a hosted-readiness
certificate. Plans and raw execution logs remain local under ignored `work/`.

## Evidence

Qualification was performed on 24 October 2026 (Asia/Kolkata) using Python
3.12.12, Node 22.23.3, Docker 29.0.1 and the existing locked dependencies.
Original-checkout reads intermittently stalled. Backend regression/type checks
therefore used an exact public-reference reconstruction plus individually hashed
current-source overlays. Targeted unit and native tests also ran in the original
checkout. No owner files were reset or deleted to work around the host issue.

The reader image was built from a whitelisted source context, immutable base and
hash-locked binary packages. Final image ID:
`sha256:17d883d732e475f175974247671d59f47a55fbf3e151c2b6185bf095f244fc31`.
Build source hash:
`989e6cc7abdc231fbf51e988bf54c4be25faee3687ee0344c5266bea3dbdee54`.

Observed checks:
- Extraction module absent, adapter contract absent and reader injection absent
  produced the expected failing tests before their implementations.
- Service oversize and incorrect adapter-byte regressions failed before explicit
  service-side bounds/digest checks, then passed (6 boundary cases).
- Current-source unit selection across findings, retrieval, agents and the new
  reader: 490 passed; 7 opt-in native cases skipped in that command.
- Strict mypy: 204 application source files passed.
- Ruff: changed application, test and build-script paths passed.
- Frontend: 22 tests passed; typing, lint and production build passed.

Separate native/database final results are recorded below after completion.
Commands use the normal locked test plugins, not a disabled-plugin diagnostic
configuration. Standard commands are `python -m pytest` with explicit affected
paths, `python -m mypy --config-file backend/pyproject.toml backend/app`, and
frontend `npm test`, `npm run typecheck`, `npm run lint`, `npm run build`.

## Limits of this evidence

The short unit selection does not include every historical integration or
performance suite. Native scanner capacity and live model/provider behavior
were not requalified. No cloud, deployment or credential configuration occurred.
The API accepts an explicitly injected reader; automatic production bootstrap
configuration is outside this change. This work does not claim new source
excerpt presentation, effective list/summary aggregation, complete MCP transport
qualification or an end-to-end deployed MVP.

## Separate native and database results

- Final reader image: **7 native tests passed**. Cases: exact extraction,
  GitHub-root mapping, wrong digest, missing member, bounded stdout overflow,
  restored owner cleanup after create, and cancellation after real container
  creation. The cancellation fixture pauses before attach; this does not claim
  a forced daemon crash during active decompression or a measured resource-OOM
  scenario. Resource controls are inspected on every real launch.
- Final real PostgreSQL/native/HTTP selection: **12 passed**. Includes grant
  revocation and retained-object deletion while a completed native read waits
  before returning; configured authenticated HTTP success; explicit unavailable
  HTTP error; prior triage concurrency, migration, support and scan-route checks.
- The first authenticated unavailable case failed because the global HTTP
  handler relabelled its error `provider_unconfigured`. After the narrow error
  allowlist repair, the complete 12-case selection passed.

Owned native containers and the marker-validated database/emulator project are
removed at handoff. Built trusted images and ignored evidence files remain;
Docker Desktop and unrelated resources stay under the owner's control.

## Subsequent integration evidence

The later combined checks, safe excerpts and current-support integration are recorded in [findings and retrieval boundary checks](retrieval-boundaries.md). Earlier counts above describe the narrower reader qualification.

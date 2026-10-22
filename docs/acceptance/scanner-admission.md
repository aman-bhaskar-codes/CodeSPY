# Scanner transport and lockfile-admission qualification

Application commit: `e754f550fbb61b54f646c1a7df120f26309a5c08`, 20 October 2026. This record qualifies contracts and admission compatibility; it does not enable scanner execution or vulnerability findings.

## Implemented behavior

Scanner request/profile/result DTOs are strict, frozen and metadata-only. Results echo the canonical digest of the entire request, including run, repository, configuration, tool/rules and selected source identities. Validation rejects unknown rules, foreign source IDs/hashes, out-of-file ranges, duplicate observations, inconsistent coverage and arbitrary raw payload fields. The broker must still attest ranges against source bytes and authenticate the active attempt.

The `source-zip-v2` policy is opt-in through trusted composition. Exact `uv.lock` files may be retained as UTF-8 TOML metadata up to 256 KiB, subject to existing path, binary, generated-directory and archive limits. V1 remains the default; its canonical hashes and omitted-version decoding are unchanged. Existing snapshots retain their original policy. Unknown policy hashes fail closed; custom policy variants require a separate registry.

Worker dispatch, sandbox decoding, retry and structural source reconstruction resolve the original registered policy. Identical bytes admitted under different policies produce distinct policy-backed snapshot identities. Lock metadata does not become a structural source dialect. No historical migration or public HTTP API changed.

## Executed checks

At the application commit above:

- Local unit/port/admission checks: **320 passed**, **5 owned-environment checks skipped locally**.
- Linux scanner-contract/admission suite: **48 passed**, including real PostgreSQL mixed-policy publication/retry/source reconstruction, fixture GitHub acquisition reuse/root stripping and actual V1/V2 admission containers.
- Structural suite: **225 passed**; ingestion compatibility at the structural schema: **45 passed**.
- TypeScript analyzer: **11 passed**. Frontend: **8 passed**, plus typing, lint, generated API drift and production build.
- Ruff, strict typing, commit hooks and secret checks passed.

[Structural/container run](https://github.com/aman-bhaskar-codes/CodeSPY/actions/runs/39384893083), [ingestion run](https://github.com/aman-bhaskar-codes/CodeSPY/actions/runs/39384893122), [database run](https://github.com/aman-bhaskar-codes/CodeSPY/actions/runs/39384893141). Owned PostgreSQL/LocalStack containers and their network were removed; cleanup passed.

Five cross-request replay regressions and an omitted-version compatibility regression were observed failing before their fixes and passing afterward. Fresh review identified the replay defect. The admission review reported no Important/Critical findings by inspection; the reviewer did not execute the tests.

## Limitations

Native scanner engines/rules, advisory processing, findings publication and triage are not enabled by these contracts. Pydantic's hidden input in exception strings does not sanitize `.errors()` or `.json()`; public/log adapters must not serialize raw validation payloads. Fixture GitHub checks are not live provider qualification. No paid model calls, real credential setup, AWS account access or deployment occurred.

See [scanner artifact research](../engineering/scanner-qualification.md) for observed positional behavior and remaining native qualification boundaries.

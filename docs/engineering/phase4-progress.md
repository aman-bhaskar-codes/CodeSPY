# Phase 4 implementation evidence

Baseline: `b13a4f5dcc961d67b53bb381801eed3f54f1cdec`. Work begins on `feat/phase4-findings` in an isolated worktree. This record describes initial transport work, not completed scanner functionality.

## Delivered increment

- P4.00: controlling roadmap adopted (one CSRF/idempotency phrase reworded to avoid a secret-scanner false positive); Phase 4 specification derived from the roadmap and repaired source seams. The earlier detailed scanner plan referenced by that roadmap was not found among supplied project files.
- P4.01a: strict metadata-only request/profile/result contracts. Full canonical request digest binds output to run, repository, configuration, tool/rules and selected file identities. Coverage partitions selected files; output cannot introduce unknown rules, foreign hashes or out-of-file ranges. Raw scanner text is not a contract field.

The trusted broker must still attest ranges against actual bytes, validate configured/qualified profiles and authenticate the attempt. A request digest is correlation, not proof that a scanner executed. Public errors/logging must not serialize raw Pydantic `.errors()`/`.json()` input payloads; hiding input in exception strings does not sanitize those structured methods.

## Personally executed checks

On 20 October 2026, using the existing pinned Python environment and TypeScript dependencies:

- Phase 4 focused contracts: **31 passed**.
- Foundation unit, Phase 2 unit/port, Phase 3 unit and Phase 4 unit checks: **295 passed**, 5.33 seconds.
- Ruff over backend application/tests and scripts: passed.
- Strict mypy over backend application: passed, 117 source files.
- TypeScript analyzer suite: **11 passed**.
- `git diff --check`: passed.

Five cross-request replay cases were observed failing before request-digest enforcement and passing after it. Fresh review found that defect; it was fixed. Other initial validation tests were developed before the implementing transport. Later boundary tests extend coverage and are not separately claimed as RED→GREEN regressions.

The new worktree initially lacked its generated TypeScript analyzer and dependency path, causing three compatibility failures. Built the existing trusted analyzer and linked existing pinned dependencies; all compatibility checks then passed. No target packages installed.

## Pending qualification

Admission V2 (including uv.lock), actual scanner binaries/rules/images, lock/advisory adapters, database schema/publication, triage/API/UI/native traces, new container qualification and GitHub CI are pending. No frontend behavior or public HTTP API changed, so no API generation or browser acceptance is claimed. No scanners, model calls, AWS account access or services were activated. Phase 4 is not complete.

## Decisions

Use a standard Git worktree because the app worktree tool returned “Not a git repository” for the nested project. Split P4.01 into transport and admission compatibility increments because current worker/retry/source policies reconstruct V1. Keep the execution ledger in ignored `work/phase4` because the master roadmap is not a helper-compatible numbered task script. These decisions affect setup and sequencing, not the preserved Phase 3 authority contracts.

## Admission-policy increment (P4.01b)

V2 is opt-in through trusted application/service composition; V1 remains the default. Exact `uv.lock` filenames may be retained as UTF-8 TOML metadata, capped at 256 KiB and still subject to generated-directory, binary, path and archive limits. Other `.lock` files remain unsupported. Admission does not parse or execute TOML. V1 default canonical hashes are fixed by regression fixtures, including GitHub root stripping. Different policies produce different policy-backed snapshot identities; existing snapshots are not reclassified.

Worker dispatch, sandbox request decoding, retries and source reconstruction resolve the original registered policy. Unknown hashes fail closed. The registry accepts only the declared default profiles; custom limits require a future persisted configuration contract. Structural source selection still excludes lock metadata as a source dialect. No historical migration changed.

Local checks for this increment: **319 passed, 5 skipped** across foundation units, Phase 2 units/ports/admission, Phase 3 units and Phase 4. The skips are two existing Linux admission tests plus two new owned-DB cases and a Linux V2 test. Ruff, strict typing and unchanged OpenAPI generation passed. Added the Phase 4 suite to the owned Linux CI path; results are pending until the pushed commit runs. This local result is not database or container qualification.

### Research used

- Read [uv's official lockfile description](https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile) in a temporary Chrome tab; then closed that tab. Universal lock contents are not assumed to be one platform's installed dependency set.
- Rechecked the public [Gitleaks v8.30.1 source tag](https://github.com/gitleaks/gitleaks/tree/v8.30.1) and [Semgrep v1.181.0 release](https://github.com/semgrep/semgrep/releases/tag/v1.181.0). These remain qualification candidates, not activated scanners.
- REA `inspect_go_binary` read `/opt/homebrew/Cellar/gitleaks/8.30.1/bin/gitleaks` without target execution. Evidence `ev_aac38ce7d1068f3b571cc273b76e76a81f99083d20b4c4c7a86de0bc26447447`: Darwin arm64, 15,301,234 bytes, SHA-256 `f414bc2fb952be6c9072b75cb411e3368614ef4b16d48dbd9ad238034afd2302`, embedded Go `go1.26.1`, main module `github.com/zricethezav/gitleaks/v8` with embedded version `(devel)`. The Homebrew directory name is not an embedded release-version attestation. This does not establish a Linux image digest, detector correctness or hostile-input isolation.

The added GitHub V2 integration case uses the fixture provider (no live GitHub sign-in) and checks pinned acquisition reuse, root stripping, publication and structural source reconstruction. It remains pending until owned Linux CI executes it. Fresh admission review found no Important/Critical issues by inspection; the reviewer did not execute checks.

Compatibility follow-up: a legacy V1 policy payload could omit its version field. A regression was observed failing after union introduction; decoding now explicitly retains V1 defaults for absent versions and rejects V2-only fields without a V2 tag. Final local aggregate: **320 passed, 5 skipped** (6.42 seconds); strict typing/Ruff pass. Phase 4 CI runs before the full structural suite for earlier failure feedback. Scanner qualification research and its limitations are recorded in [scanner notes](phase4-scanner-research.md).

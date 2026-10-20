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

# Review query verification

Observed on 23 October 2026. These results cover the current scan-status, finding-support and triage/query repairs, plus the migration compatibility fixes needed to run them. They do not establish deployment, live model behavior or complete native scanner qualification.

| Check                                      | Observed result                                                     |
| ------------------------------------------ | ------------------------------------------------------------------- |
| Backend unit suites                        | 729 passed, 15.35 seconds                                           |
| PostgreSQL migration/API-role regressions  | 8 passed, 10.22 seconds                                             |
| Frontend Vitest                            | 22 passed across 7 files, directly in the checkout                  |
| TypeScript analyzer                        | 11 passed after its exact-lock install/build                        |
| Strict mypy                                | Passed, 197 application files                                       |
| Ruff                                       | Passed for application, scanner tests and changed migration harness |
| Frontend typing, lint and production build | Passed                                                              |
| Migration chain                            | Complete upgrade, empty-command downgrade and re-upgrade passed     |
| Source contract                            | Current commit plus recorded working-source overlays                |

The PostgreSQL suite uses real API, broker and migration roles on PostgreSQL 16. It exercises concurrent identical triage commands producing one event and one response; downgrade refusal with retained responses and revision identity intact; current grants and competing reviewers; deleted source-record support downgrade; and scoped status reads. Each case uses an owned disposable database. It does not replace the historical native scanner or capacity evaluations.

The complete migration chain exposed two compatibility failures: later migrations reference a grant role absent from the old test setup, and an existing descriptive revision ID exceeds Alembic's default 32-character column. The owned test setup now supplies `codespy_app` as a `NOLOGIN` role with no memberships or privilege flags. API and broker users do not inherit it. The migration environment creates or widens its ledger to 64 characters transactionally, without changing historical revision IDs or migration files. Real regressions cover an existing 32-character column and downgrade/re-upgrade behavior.

## Environment and provenance

The old shared frontend installation stalled while reading a jsdom dependency. A separate installation of the unchanged lockfile passed all tests, then replaced only this checkout's dependency symlink. The previous shared link was preserved; its target was not modified. Node 22.23.3 was used for tests and builds.

Backend startup also encountered blocked reads in existing package metadata, bytecode, source files and Git objects. Qualification therefore used a clean Python 3.12.12 environment from the frozen workspace export and a fresh archive of `6f131562182268e333463d52c33c3af38a50bc83`, overlaid with the current modified and new source/test files. [Recorded overlay hashes](review-query-sources.json) identify the tested implementation. The final backend and PostgreSQL runs used normal plugin loading. Earlier diagnostic runs that disabled discovery or failed before collection are not acceptance evidence.

An initial backend unit run in the source snapshot failed three analyzer-dependent cases because the generated TypeScript sidecar was absent. Installing its unchanged lockfile and running its build/tests resolved that setup issue; the subsequent 729-test run passed. A PostgreSQL regression initially contained an extra fixture argument; it was corrected before the eight-test passing run.

Local filesystem stalls in the original backend checkout remain a host-environment limitation. The snapshot results establish behavior for the recorded source, not a claim that those original file reads are repaired. The blanket backend test command also includes separately configured historical integration environments; this record does not turn earlier missing-environment errors or opt-in skips into passes.

Owned PostgreSQL containers and their network were removed using marker-validated cleanup. Developer volumes and unrelated Docker resources were preserved. No AWS account, provider credentials, paid API calls or deployment were used. Implementation remains local until separately approved GitHub delivery.

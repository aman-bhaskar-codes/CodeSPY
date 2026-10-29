# Workspace integration verification

Observed on 2026-10-28 against checkout HEAD `6572c937b1869d495372c53059aade525ae1ae04` and local working changes. Verification applies to this working tree, not a published release.

## Implemented behavior

- Focused hash-addressed views for overview, source intake, files, structure, findings, search/context/memory and investigations; inactive panes preserve local drafts.
- Dark default and persistent light appearance, self-hosted licensed IBM Plex fonts and shared semantic tokens.
- Archive repository creation and explicit Git reference acquisition for existing server-bound GitHub repositories. Provider registration and credentials remain owner responsibilities.
- Scoped scan configuration, create, status, cancellation and principal-bound retry through existing job services. Findings use the published result run identity, and investigation links retain finding/run scope.
- Reviewed-memory controls, source-linked dialogs, context budgets, index progress and explicit partial coverage through existing backend contracts.
- Model configuration availability, explicit investigation execution, recovery actions, published citation checks, saved-result reload and bounded paginated event/history views.
- Shared accessible dialogs, keyboard tabs, responsive navigation and persistent inline recovery notices.
- Session reset clears private caches; stale access responses cannot publish private results. History changes fence late source-job selection responses.

## Verification record

Detailed command outputs are retained locally under `work/frontend-foundation/`.

| Final local check                      | Observed result             |
| -------------------------------------- | --------------------------- |
| Frontend Vitest                        | 23 files, 66 tests passed   |
| Frontend TypeScript                    | Passed                      |
| ESLint with zero warnings              | Passed                      |
| Production Vite build                  | Passed                      |
| Temporary-profile Chrome fixture tests | 12 passed; HTTP intercepted |
| Strict UI static audit                 | Zero findings               |
| Ruff on changed API/integration files  | Passed                      |
| Combined diff whitespace check         | Passed                      |

The browser matrix includes investigation recovery at 390 and 1440 pixels in
both themes and memory review at 390 and 1280 pixels. Dialog focus cycling,
nested triage return focus, cancelled-draft reset, late response fencing and
history restoration also have component regressions.

Real connected checks observed during this work:

| Check                         | Observed result                                                 | Qualification boundary                                                                                                             |
| ----------------------------- | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Index browser integration     | One passed                                                      | Real PostgreSQL 16, isolated source reader/indexer and indexing worker; starts from a trusted admitted fixture; queue is in memory |
| Scoped scan lifecycle HTTP    | One passed                                                      | Real PostgreSQL; scanner capability registry is synthetic, no native scanner-quality claim                                         |
| Investigation/memory recovery | Six passed                                                      | Real PostgreSQL, encrypted execution effects and isolated source reader; provider exchanges are synthetic                          |
| MCP connected checks          | Seven passed plus one separately qualified isolated-reader case | Real PostgreSQL; some index preparation is synthetic                                                                               |
| Complete local journey        | One passed                                                      | Real PostgreSQL 16, isolated Docker source reader and indexer; single end-to-end journey from archive upload and admission through structural analysis, security scanning, exact/dense retrieval, reviewed-memory promotion to saved investigation execution and recovery (`backend/tests/journey/test_connected_journey.py`) |
| Backend strict typing         | Passed, 289 source files                                        | Separate verification environment restored from the unchanged frozen lockfile                                                      |

The broader offline backend suite passed; eight guarded storage checks in that run were skipped and qualified separately where listed above. Mocked provider tests establish plumbing and recovery, not answer quality.

Browser tests with intercepted HTTP cover both themes, narrow layouts, keyboard/focus behavior, accessibility scans, local-only assets, memory review and investigation execution/reload. They do not establish live provider or cloud connectivity. Automated accessibility scans do not replace manual assistive-technology review.

## Configuration and remaining qualification

Scan controls require a registered server-owned `scan_defaults` template and matching capabilities; absence is an explicit unavailable state. A configuration response never probes scanner or model health. GitHub acquisition requires an existing authorized server-side binding.

The single connected backend journey (upload → native analysis/scan → index → investigation → saved-result) is qualified against real PostgreSQL and isolated Docker runtimes (`test_connected_journey.py`). Cloud services, live model access/quality, manual screen-reader review and deployment remain unqualified. No owner credentials, AWS access, paid model calls, deployment or GitHub mutation were used.

## Delivery verification — 29 October 2026

Fresh local checks on the delivery working tree passed 66 component tests and 20
intercepted-HTTP browser cases, including the 320/390/768/1440-pixel reflow matrix.
The new responsive fixtures initially omitted the published scan-job/run identity
and used an ambiguous memory-topic selector; both were corrected. The 320-pixel
memory filter row also reproduced document overflow and now wraps its controls.
The acceptance assertions remain enabled. Generated browser reports are excluded
from lint, not from source control checks alone.

Frontend typing, lint, production build and API generation consistency passed.
Affected offline backend suites passed 824 cases; strict application typing passed
for 289 files and changed-file Ruff passed. These local results do not replace the
new Linux PostgreSQL/container integration gate. Live providers and hosted deployment remain unqualified. Development is
paused after reviewed delivery; the owner handles credentials and deployment.

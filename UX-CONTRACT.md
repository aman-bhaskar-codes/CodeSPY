# CodeSPY interaction contract

Sources: backend/openapi.json, backend/app/contracts/ingestion.py and backend/app/contracts/jobs.py. Application services and database guards enforce lifecycle and permissions.

## Canonical UI Map

| Capability     | Canonical owner                         | Source of truth      | Allowed variants                 | Verification                     |
| -------------- | --------------------------------------- | -------------------- | -------------------------------- | -------------------------------- |
| Select/Listbox | Native select in App                    | DESIGN.md            | Native platform popup            | App.test.tsx; browser unexecuted |
| Form           | App upload form                         | Generated API contract  | ZIP upload                       | App.test.tsx                     |
| Scrollbar      | styles/app.css                          | DESIGN.md            | Global visible baseline          | Static audit; browser unexecuted |
| Toast          | App persistent status and alert regions | UX-CONTRACT.md       | Status, error                    | App.test.tsx                     |
| CRUD           | api/client.ts and App                   | backend/openapi.json | Submit, cancel, retry, inventory | Backend API tests                |

Session unavailable: show sign-in/retry and no write controls. Repository list empty: explain no accessible repositories. Upload: reserve, stream raw ZIP, observe job; idempotency key belongs to one command. Unknown completion: retain identifiers and reload before offering a fresh command. Job terminal state ends polling; inventory child is separate. Inventory uses scoped cursor pages and explicit Next/Previous. Cancel requires a labeled in-page confirmation; focus returns to the action. No hard deletion UI.

Async requests receive AbortSignal; job/repository changes isolate query keys. Session expiration preserves the selected file, disables writes and offers sign-in. Selected submission, job and snapshot UUIDs persist in URL parameters. Retry command keys persist in sessionStorage to recover ambiguous responses; source bytes and tokens do not. Empty, loading, partial and excluded states use plain text. Inventory is not analysis coverage. Keyboard/browser/zoom checks remain pending until authorized.

## Source indexing and retrieval

Sources: backend/app/contracts/indexing.py, backend/app/services/index_jobs.py, and the generated OpenAPI index endpoints. The source-index panel reuses the existing panel, native button, inline confirmation, status and alert conventions. Server-returned `can_build`, `can_cancel` and `can_retry` flags control available actions; the browser does not infer ownership or role permissions.

Build and retry commands retain one request key until a response confirms the job identity. Only command keys and job UUIDs are retained in sessionStorage; no source text, query, provider credentials or evidence content is stored there. Terminal jobs stop polling, refresh index availability and preserve existing result sets. Failed refreshes show a reload action rather than indefinite polling. Cancellation requires an in-page confirmation and does not remove a published index.

Published set identity, coverage and omissions appear before search. An unavailable index differs from an available set with no matches. Search and context results display their own set identities, including while a newer index builds. Pinning a search hit as memory carries its qualified chunk identity and content digests. Editing its file or range clears that chunk association. Search queries and draft memory text remain transient because analyzed source identifiers may be sensitive; they are not persisted in URLs. Generated API types own transport fields and supported memory/chunk kinds.

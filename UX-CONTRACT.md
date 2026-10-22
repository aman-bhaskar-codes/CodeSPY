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

# CodeSPY interaction contract

## Product context

CodeSPY helps developers inspect admitted source, review findings and investigate
questions using traceable evidence. English is the current interface language.
The target is WCAG 2.2 AA. This contract describes interface behavior; the backend
contracts and current authorization remain authoritative for permissions and data.

## Authority and source contracts

- `backend/openapi.json`: public transport shapes and operations.
- `backend/app/security/authorization.py`: current repository and snapshot authority.
- `backend/app/contracts/memory.py`: reviewed-memory lifecycle and provenance.
- `backend/app/contracts/investigation_jobs.py`: durable job and recovery behavior.
- `backend/app/contracts/source_view.py`: safe source representations and ranges.
- `DESIGN.md`: visual intent and runtime token ownership.

Do not infer permissions, completed work or financial certainty from visual state.
Do not expose credentials, raw provider payloads or source-bearing diagnostics.

## Canonical UI Map

| Capability                    | Canonical owner                                          | Source of truth                                      | Allowed variants                                           | Verification                                              |
| ----------------------------- | -------------------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------- | --------------------------------------------------------- |
| Appearance                    | `frontend/src/theme/ThemeProvider.tsx`                   | `frontend/src/styles/tokens.css`                     | Dark default, light; storage optional                      | Theme component and browser tests                         |
| Workspace chrome              | `frontend/src/workspace/WorkspaceShell.tsx`              | This navigation contract                             | Enabled links or explained unavailable destinations        | Shell component and browser tests                         |
| Selection restoration         | `frontend/src/App.tsx`                                   | Validated URL identifiers; backend authority         | Repository, snapshot, submission, job, scan                | App restoration and browser tests                         |
| Buttons                       | `frontend/src/ui/Button.tsx`                             | `DESIGN.md`                                          | Neutral, primary, danger; pending                          | Button component tests                                    |
| Panels                        | `frontend/src/ui/Panel.tsx`                              | `DESIGN.md`                                          | Labelled section, optional actions                         | Browser checks of session recovery                        |
| Status                        | `frontend/src/ui/StatusBadge.tsx`                        | `DESIGN.md`                                          | Neutral, success, warning, danger                          | Shell and browser checks                                  |
| Global scrollbar              | `frontend/src/styles/app.css`                            | Runtime tokens                                       | Theme and forced colors                                    | Narrow browser layout checks; forced colors remain manual |
| Existing form/table workflows | Current feature components                               | Existing feature contracts                           | Shared semantic tokens; existing transport contracts       | Feature tests and browser checks                          |
| Select/Listbox                | Native HTML select, used in App and feature forms        | Browser/OS keyboard and popup behavior               | OS-owned popup; labels required                            | App and workbench tests                                   |
| Form                          | Native forms in App and feature components               | Generated API constraints and local field validation | noValidate, inline correction, disabled pending submission | Upload, triage and memory tests                           |
| Scrollbar                     | frontend/src/styles/app.css                              | frontend/src/styles/tokens.css                       | Document scroll, table overflow, forced colors             | Narrow browser and layout checks                          |
| Toast                         | frontend/src/ui/InlineNotice.tsx                         | This interaction contract                            | Persistent inline notices; no ephemeral toast subsystem    | Memory and investigation tests                            |
| CRUD                          | frontend/src/api/client.ts and scoped feature components | backend/openapi.json and current authorization       | Explicit commands, request keys and recovery views         | Frontend contract tests and connected browser checks      |

New dialogs, notifications, selects and route transitions require a named shared
owner and keyboard tests before they are introduced. Do not create equivalent
screen-local owners. Existing feature controls are not automatically certified by
this foundation contract.

## Navigation and context

Section links point only to mounted, focusable headings. Unavailable views must not
start hidden operations. The shell does not fabricate repository or snapshot data;
it uses actual selected values, or states that no value is selected.

Repository, submission, job, scan and snapshot identifiers restore from validated
URL parameters. Browser history restores those selections and resets inventory
pagination; section navigation preserves local form input. The home link targets
the mounted overview rather than reloading the application. URLs never contain
source, questions or memory text. Backend authorization remains authoritative.
Browser navigation alone must not cancel durable jobs. Pane links use validated hashes; dedicated repository paths and resizable panels
are not implemented. Saved investigation and finding/run identifiers also restore
from the URL, scoped to the selected repository and snapshot.

## Interaction and failure

- Buttons default to `type="button"`; submit is explicit.
- Busy buttons preserve their accessible name and prevent duplicate activation.
- Disable unavailable navigation with a visible reason; never treat UI state as authorization.
- Existing uncertain mutations keep their request/status recovery behavior.
- A session failure shows an actionable inline error and a sign-in/retry path.
- Stable loading, empty and error states must not replace the main navigation.
- Avoid native product alert/confirm/prompt dialogs; use an approved accessible shared owner.
- Critical errors remain inline rather than appearing only in a toast.

## Sensitive state

Persist only theme and other explicitly approved harmless presentation preferences.
Do not put credentials, source excerpts, retrieval queries, model prompts, memory
text or cached results in local storage. Session expiry clears private caches and unmounts the workspace. Responses started
under the previous access revision are rejected before publication. Failed scoped
reads remove previously cached data; write denial does not globally sign out. Appearance preferences do not establish a session.

## Responsive and focus behavior

The workspace uses normal document scrolling. Navigation remains visible and
labelled at narrow widths, with horizontal scrolling limited to its own strip.
Long forms and table panels keep separate scroll responsibilities. Focus styles
remain visible, including on skip links and controls. Route/pane migrations must
verify 200% zoom, keyboard access and phone layouts before being called complete.

## Dialogs, progress and bounded history

`frontend/src/ui/Modal.tsx` owns focus containment, Escape, busy dismissal guards
and return focus for drawers and dialogs. Triage is mounted per opening so a
cancelled draft cannot silently carry into a later review version. Investigation
creation saves the request; execution is explicit and may use a configured model.
Model availability describes configuration, never live access qualification.

Investigation history and events use server cursors. The timeline shows one saved
page and at most 200 recent live events, with an explicit coverage notice. Report
source correspondence describes checks at publication; it does not establish a
security conclusion or continuing source availability.

## Qualification boundary

See [workspace verification](docs/acceptance/frontend-workspace.md) for the recorded
component/browser results and remaining manual accessibility checks. This document
describes implemented design and interaction contracts, not a hosted release or
complete upload-to-investigation qualification.

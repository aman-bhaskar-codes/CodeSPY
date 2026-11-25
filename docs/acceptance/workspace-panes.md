# Acceptance: workspace panes and large-component split

Scope: `App.tsx` is composition only; each pane is a component registered in one place; the two oversized workbenches
(investigations, retrieval) are split into a controller hook and parts. No behaviour changed: the existing unit and
browser tests pass without modification.

## What changed

| Before                                                         | After                                                                                                                                                                                                                                             |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `App.tsx`, 1,197 lines of state, queries, mutations and markup | `App.tsx`, 53 lines: the shell, the session notices and the pane registry                                                                                                                                                                         |
| Panes written inline in `App.tsx`                              | One component per pane in `frontend/src/workspace/panes/`, listed in `registry.tsx`                                                                                                                                                               |
| Activity bar and palette destinations written by hand          | Derived from the registry, in the same order and with the same enabled rules                                                                                                                                                                      |
| Selection, session, queries, commands in one function          | Hooks by concern in `frontend/src/workspace/` (`useWorkspaceSelection`, `useSessionData`, `useJobStatus`, `useInventory`, `useAnalysis`, `useScan`, `useIntake`, `useJobControl`) composed by `useWorkspaceController` and shared through context |
| `InvestigationsWorkbench.tsx`, 2,125 lines                     | 55-line composition, a controller hook, and 14 parts under `features/investigations/parts/` (form, history, detail header, claims, timeline, feedback and others)                                                                                 |
| `RetrievalWorkbench.tsx`, 1,499 lines                          | 58-line composition, a controller hook, and 5 parts under `features/retrieval/parts/` (heading, search, context, memory, source drawer)                                                                                                           |

Adding a pane is one registry entry. The code inside each part is moved, not rewritten: the move was done by line range
so that behaviour is identical by construction, and only import lists, the names of refs returned from hooks (the lint
rule requires the `Ref` suffix) and the narrowing of a selected investigation changed.

## Verification

```
npm --prefix frontend run typecheck && npm --prefix frontend run lint
npm --prefix frontend run test              # 161 tests, unchanged
npm --prefix frontend run test:e2e:fixture  # 27 tests, unchanged
npm --prefix frontend run build
```

## Bundle size

Main chunk before: 508,887 bytes (146,258 gzip). After: 519,082 bytes (149,324 gzip), +2.0% raw and +2.1% gzip, a little
over the 2% target. Property names are kept across module boundaries (each controller returns its names and each part
destructures them), which accounts for most of it. Panes can now be loaded lazily from the registry, which is the way to
bring the initial bundle down; that is left to the editor work, where Monaco must be lazy anyway.

## Known limits

- The two controller hooks are still large (about 550 and 390 lines). They are cohesive state and commands and can be
  divided further when behaviour in them next changes.
- A few large parts remain (memory tab about 440 lines, search tab about 400).

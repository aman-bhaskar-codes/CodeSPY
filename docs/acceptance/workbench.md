# IDE-style workbench

The frontend is laid out like a code editor workbench (the interaction model of VS Code, built in our own
React code with our own name, icons and fonts; no Microsoft code or assets).

## Parts

| Part | Owner | Behaviour |
|---|---|---|
| Title bar | `workspace/WorkspaceShell.tsx` | Brand, repository, session, theme, sign out |
| Activity bar | same, `nav` named "Workspace sections" | Icon links for the existing destinations; unavailable ones explained by a hint, not linked |
| Explorer | `features/explorer/FileTree.tsx` | `role="tree"` built from the snapshot inventory; arrows, Home/End, Enter, typeahead; loads more pages on request and says when the listing is partial |
| Editor tabs | `workspace/EditorTabs.tsx` | `role="tablist"` of visited views and open files; the URL hash stays authoritative; Delete, Backspace or middle click close a tab |
| Source editor | `features/source/SourceEditor.tsx` | Read-only numbered lines from the redacted `model_safe` source view; omitted or unreadable files are explained |
| Bottom panel | `workspace/BottomPanel.tsx` | Problems (alerts shown anywhere in the editor) and Output (status messages), collected by a MutationObserver so screens are unchanged |
| Status bar | `WorkspaceShell.tsx` | Session, repository, snapshot, active view, problem count, Commands, Panel |
| Command palette | `workspace/CommandPalette.tsx` | Ctrl/Cmd+Shift+P or F1; fuzzy filter; go to enabled views, toggle explorer or panel, theme |
| Splitters | `workspace/Splitter.tsx` | `role="separator"` with value, min and max; drag, arrow keys, Home/End, Enter or double click to reset; widths persist |

## Keyboard

Ctrl/Cmd+B explorer, Ctrl/Cmd+J panel, Ctrl/Cmd+Shift+P or F1 palette, F6 and Shift+F6 cycle the parts
(activity bar, explorer, editor, panel, status bar; hidden parts are skipped). Shortcuts never fire while typing
in a field or inside another dialog.

## Accessibility and responsiveness

Axe (WCAG 2.0 to 2.2 A and AA) passes in dark and light with the panel and palette open. A tablist owns only tabs
(the close glyph is a mouse affordance; keyboard users press Delete). Below 760 px the activity bar becomes a strip,
the explorer stacks under it, the panel stays usable and the page never scrolls horizontally down to 320 px.
Forced-colors and reduced-motion are respected.

## Visual language

VS Code-like dark and light tokens in `styles/tokens.css` (editor, side bar, activity bar, status bar, tabs),
13 px interface text and 22 px rows, IBM Plex fonts self-hosted (no third-party origin).

## Not included

- Monaco: deferred. It is installed but loads from a CDN by default, which the local-assets test forbids; adopting
  it needs local bundling and workers and a read-only wrapper for the redacted representations.
- Multiple editor groups and drag-to-split; terminal; extensions.
- `App.tsx` was not split into pane components. The explorer attaches through a shell slot instead, which kept
  `App.test.tsx` unchanged. Extracting the intake, job, inventory and scan screens remains a later refactor.
- The explorer lists at most 2,000 files (40 pages) and says so.

## Verification

Vitest: 124 tests (shell, splitter, layout persistence including blocked storage, keyboard parts, tree, source
editor, tabs, panel, palette, rule checks). Playwright fixture suite: 27 tests including `tests/e2e/workbench.spec.ts`
(named parts, F6, splitter persistence, Ctrl+B, palette, panel, axe with panel and palette open, 320 px overflow).
Two existing locators for the "Memory" tab became exact because the editor strip now also contains "Search & memory".

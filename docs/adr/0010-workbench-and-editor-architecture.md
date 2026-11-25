# ADR 0010: Workbench and editor architecture

Status: accepted. Nothing here needs a live model or a cloud account. Builds on
[ADR 0008](0008-agent-runtime-and-execution-policy.md); the stream contract it consumes is in
`docs/contracts/AgentEventV1.schema.json`.

## Context

The product is a VS Code-style IDE in the browser with an agent beside the code. The frontend already has a tested,
accessible React workbench (activity bar, explorer, tabs, panel, command palette, splitters) and design rules in
`UX-CONTRACT.md` and `DESIGN.md`. The question is whether to keep building on it or adopt a full VS Code web build.

## Decisions

1. **Keep the React workbench and add plain Monaco, bundled locally.** The differentiators (evidence with provenance,
   the agent timeline, reverse-engineering views) are custom surfaces that fit React directly. VS Code familiarity comes
   from layout, keybindings, quick open, the command palette and Monaco itself, all of which are matched here.
   Monaco is a pinned dependency, loaded lazily with its workers through the bundler; no editor asset comes from a
   third-party origin, and the existing test that forbids off-origin requests stays mandatory.
   Rejected: OpenVSCode Server and code-server (would discard the current workbench, contract and tests, and
   run one server per session), Eclipse Theia (heavy framework, own dependency injection). A later spike may evaluate
   `monaco-vscode-api` for TextMate grammars and extensions; it needs a second origin for the extension host.
2. **Layout follows VS Code.** Activity bar, primary sidebar (explorer, search, source control, agents, evidence,
   reverse engineering), editor groups with splits, a secondary sidebar on the right for the agent panel, a bottom
   panel (problems, output, terminal, tests, agent log) and a status bar. The agent is never a modal or a separate page.
3. **State.** TanStack Query for request and response data; Zustand for workbench UI (layout, tabs, focus); one
   append-only event store per agent session with a pure reducer to a view model and memoised selectors. Derived
   stream state is not kept in the query cache.
4. **Persistence.** The server is the source of truth for sessions, events and workspace. Only harmless presentation
   preferences persist in the browser (theme, sizes, keybinding overrides, panel visibility). Unsent drafts stay in
   memory; no prompts, source or memory text in storage or URLs. A session is addressed by an identifier only.
5. **Streaming client.** One `EventSource` per open session, resuming with `Last-Event-ID`. The reducer is idempotent
   by sequence number and by message-delta offset; a `stream.reset` makes the client reload the session state.
   A client refuses an event of a protocol version it does not speak and ignores event types it does not know.
6. **Protocol types are generated, never hand-written.** `frontend/src/features/agent/protocol/types.gen.ts` is
   generated from the backend's JSON Schema; a test fails if it is out of date, and the same test validates recorded
   sessions against the schema. Recorded sessions (a normal run, a rejected run, a run interrupted by a lost worker)
   are produced by the backend contracts and are replayed by both the backend and the frontend tests.
7. **Diff review** uses Monaco's diff editor with per-hunk accept and reject, keyboard-operable.
8. **Terminal** is xterm.js over a WebSocket to a real session sandbox; agent command output renders in a read-only
   instance from stream events. A terminal never simulates a shell.
9. **Language intelligence** starts with syntax highlighting and evidence-backed navigation from existing structural
   facts. Language servers run inside the session sandbox and are reached over a WebSocket; none run in the API.
10. **Agent markdown** is the only rendered user-visible markup from the model: parsed with raw HTML disabled,
    sanitised with a strict allowlist, links limited to `https:` and in-app routes, code shown as text.
11. **Virtualisation** (`@tanstack/react-virtual`) for the explorer, search results, long timelines, and hex and
    disassembly lists.
12. **Keybindings** go through one registry (command id, default key, when-clause) shared by the palette and menus,
    with VS Code defaults and user overrides stored as a harmless preference.
13. **Strings** in new components come from message catalogs from the settings work onward; English only at launch.

## Consequences

- The tested workbench and its accessibility guarantees are kept. Extension compatibility with VS Code is not offered.
- Every new surface needs an owner row in `UX-CONTRACT.md` and keyboard and axe coverage before it ships.
- Dependency additions are limited to those named above and are pinned.

## Not decided here

Whether `monaco-vscode-api` is adopted (a spike, after the editor is in place), the exact content security policy
(verified when Monaco is bundled), and telemetry wording.

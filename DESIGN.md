---
version: alpha
name: CodeSPY
description: An evidence-focused source analysis workspace with restrained IDE navigation.
colors:
  action: "#91B4FF"
  canvas: "#11151B"
  panel: "#171D26"
  text: "#E7EDF5"
  muted: "#A2AFC1"
  evidence: "#6BC7BC"
typography:
  sans:
    fontFamily: '"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif'
  mono:
    fontFamily: '"IBM Plex Mono", ui-monospace, monospace'
rounded:
  DEFAULT: "6px"
spacing:
  control-height: "40px"
  panel-padding: "24px"
  workspace-padding: "32px"
components:
  button:
    owner: frontend/src/ui/Button.tsx
  panel:
    owner: frontend/src/ui/Panel.tsx
  status:
    owner: frontend/src/ui/StatusBadge.tsx
---

# CodeSPY visual system

## Intent

A quiet engineering workspace: clear source identity, persistent orientation and
visible evidence. IDE familiarity comes from contextual navigation and restrained
surfaces, not simulated terminals or decorative activity. Product copy describes
implemented behavior without claims of certainty the system cannot establish.

## Canonical tokens and themes

`frontend/src/styles/tokens.css` is the runtime token authority. This document
records the accepted intent; do not maintain an independent theme in JavaScript,
a second component library or screen-local color constants. Shared components use
`--cs-*` roles. Existing `--ink`, `--surface`, `--action`, `--muted`, `--border` and
`--danger` variables adapt those roles for existing application markup.

Dark is the initial theme. Light is an explicit user choice. Only the harmless
appearance preference is persisted under `codespy.appearance.theme`; storage failure
must not prevent rendering. Native controls follow the selected color scheme.
Forced colors use system colors, and reduced motion disables decorative transitions.

| Semantic role     | Dark      | Light     |
| ----------------- | --------- | --------- |
| Canvas            | `#11151B` | `#F3F5F8` |
| Panel             | `#171D26` | `#FFFFFF` |
| Raised surface    | `#202938` | `#E9EEF5` |
| Divider           | `#344154` | `#C5CFDC` |
| Main text         | `#E7EDF5` | `#1B2533` |
| Muted text        | `#A2AFC1` | `#526174` |
| Action            | `#91B4FF` | `#254FC7` |
| Action foreground | `#11151B` | `#FFFFFF` |
| Evidence accent   | `#6BC7BC` | `#0F766E` |

Semantic success, warning and danger tokens augment a textual status. They never
substitute for a status label. Divider contrast is not the control-boundary token;
interactive fields and buttons use the stronger control border.

## Typography and assets

IBM Plex Sans is used at 400, 500 and 600; IBM Plex Mono at 400. Self-hosted WOFF2
files and the SIL Open Font License live in `frontend/public/fonts`. No runtime
font CDN is used. Body text remains a readable sans-serif; identifiers and hashes
use the monospace family. Long names wrap instead of silently hiding identity.

## Layout and behavior

The shell owns the product header, navigation, main landmark and footer. Main
content keeps document scrolling; no ancestor hides overflow to force tables into
a viewport. Navigation becomes an overflow-visible horizontal strip below 760px.
Labels remain visible. The smallest header layout wraps its controls rather than
letting controls push the document beyond the viewport.

The shell uses hash-addressed focused panes. Inactive workbenches stay mounted to
preserve drafts while remaining hidden from navigation and assistive technology.
Overview presents intake and job progress. This is not a pathname router or a
resizable file explorer. Unavailable destinations
are labelled text with a visible explanation, not nonfunctional links.

## Shared visual states

- Button: neutral, primary or danger intent; hover, focus, active, disabled and busy.
- Busy controls retain their accessible name and geometry; repeated activation is blocked.
- Panel: labelled section with an optional action area and wrapping content.
- Status: compact textual badge; no implicit live region on every count or badge.
- Focus: visible action-color outline with offset; keyboard access is not replaced by hover.

## Feature surfaces and shared interactions

Intake, findings, structure, retrieval, reviewed memory and investigations use the
same semantic theme tokens. Radix Dialog owns drawers, triage and cancellation
confirmation; Radix Tabs owns the search/context/memory switcher. Native controls
retain their browser keyboard behavior. Persistent inline notices carry recovery
instructions rather than timed notifications. Both themes retain source identity,
partial coverage, pending operations and explicit unavailable states.

## Verification

Theme behavior, storage denial, disabled navigation and busy buttons have component
contracts. Browser checks cover both themes, narrow layouts, focus, axe results,
self-hosted fonts and the existing application entry states. Automated checks do
not establish full product accessibility without the connected workflow checks.

## Qualification boundary

See [workspace verification](docs/acceptance/frontend-workspace.md) for the recorded
component/browser results and remaining manual accessibility checks. This document
describes implemented design and interaction contracts, not a hosted release or
complete upload-to-investigation qualification.

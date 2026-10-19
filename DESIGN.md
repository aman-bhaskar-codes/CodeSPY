# CodeSPY interface

## North star

A source inspection desk for developers reviewing what an archive contains. The primary evidence is the file inventory, including exclusions. Avoid marketing dashboards, decorative metrics, and claims of semantic analysis.

## Register and audience

Product tool, English, developers using a desktop or narrow laptop. Phase 2 exposes source admission and inventory only. No established sibling screens exist yet.

## Tokens and ownership

Runtime owner: frontend/src/styles/app.css. Navy ink #18304b, cobalt action #2459c5, cool canvas #f2f5fa, white surface #ffffff, muted text #52647a, border #c7d2e1, danger #9c2929. Body uses system sans; identifiers use system monospace. No downloaded fonts. Spacing follows 6/12/18/24/36px; panels have 8px corners. One signature: a wide, ruled file inventory with hashes and explicit inclusion decisions.

## Layout

Header with session status; repository and upload controls above the current job; inventory below. Maximum width 1200px, document scrolling, table owns horizontal overflow. Native select popup geometry is intentionally platform-owned.

## Behavior and accessibility

Visible focus, labeled native controls, stable status region, no decorative animation. Text accompanies all status colors. UI contract: UX-CONTRACT.md. No browser automation is authorized; runtime visual and browser accessibility checks remain unexecuted.

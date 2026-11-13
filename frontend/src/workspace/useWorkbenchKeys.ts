import { useEffect } from "react";

const PARTS = ["activity", "sidebar", "editor", "panel", "status"] as const;
export type WorkbenchPart = (typeof PARTS)[number];

function editable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) ||
    target.closest("[role='dialog']") !== null
  );
}

/** The part of the workbench that currently holds focus, if any. */
function currentPart(root: HTMLElement): WorkbenchPart | null {
  const active = document.activeElement;
  if (!active) return null;
  for (const part of PARTS) {
    const el = root.querySelector(`[data-part="${part}"]`);
    if (el && el.contains(active)) return part;
  }
  return null;
}

export function focusPart(root: HTMLElement, part: WorkbenchPart) {
  const el = root.querySelector<HTMLElement>(`[data-part="${part}"]`);
  if (!el) return;
  const first =
    part === "activity" ? el.querySelector<HTMLElement>("a[href]") : null;
  (first ?? el).focus();
}

/**
 * Workbench shortcuts: Ctrl/Cmd+B toggles the explorer, F6 and Shift+F6 move between parts.
 * Typing in fields and dialogs is never intercepted.
 */
export function useWorkbenchKeys(
  rootRef: { current: HTMLElement | null },
  toggleSidebar: () => void,
  sidebarVisible: boolean,
  togglePanel: () => void,
  panelVisible: boolean,
) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const root = rootRef.current;
      if (!root) return;
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.shiftKey &&
        !event.altKey &&
        event.key.toLowerCase() === "b" &&
        !editable(event.target)
      ) {
        event.preventDefault();
        toggleSidebar();
        return;
      }
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.shiftKey &&
        !event.altKey &&
        event.key.toLowerCase() === "j" &&
        !editable(event.target)
      ) {
        event.preventDefault();
        togglePanel();
        return;
      }
      if (event.key === "F6" && !event.ctrlKey && !event.metaKey) {
        if (
          event.target instanceof HTMLElement &&
          event.target.closest("[role='dialog']")
        )
          return;
        const parts = PARTS.filter(
          (p) =>
            (p !== "sidebar" || sidebarVisible) &&
            (p !== "panel" || panelVisible),
        );
        const now = currentPart(root);
        const index = now ? parts.indexOf(now) : -1;
        const next =
          parts[
            (index + (event.shiftKey ? -1 : 1) + parts.length) % parts.length
          ];
        event.preventDefault();
        focusPart(root, next);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [rootRef, toggleSidebar, sidebarVisible, togglePanel, panelVisible]);
}

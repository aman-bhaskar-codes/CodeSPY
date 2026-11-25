import { useEffect, useState } from "react";

export interface LogEntry {
  at: string;
  text: string;
}

const MAX_LOG = 100;

function texts(root: ParentNode, selector: string): string[] {
  const found = new Set<string>();
  for (const el of root.querySelectorAll(selector)) {
    if (el.closest("[hidden]")) continue;
    const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
    if (text) found.add(text.slice(0, 300));
  }
  return [...found];
}

/**
 * Watches the editor area for alerts (the Problems list) and status messages (the Output log), so
 * every screen feeds the panel without being changed. Panel content itself lives outside `root`.
 */
export function useWorkbenchObserver(root: { current: HTMLElement | null }) {
  const [problems, setProblems] = useState<string[]>([]);
  const [output, setOutput] = useState<LogEntry[]>([]);
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    let frame = 0;
    const seen = new Set<string>();
    const scan = () => {
      frame = 0;
      const alerts = texts(el, "[role='alert']");
      setProblems((old) =>
        old.length === alerts.length && old.every((t, i) => t === alerts[i])
          ? old
          : alerts,
      );
      const fresh = texts(el, "[role='status']").filter((t) => !seen.has(t));
      if (fresh.length) {
        const at = new Date().toLocaleTimeString();
        for (const t of fresh) seen.add(t);
        setOutput((old) =>
          [...old, ...fresh.map((text) => ({ at, text }))].slice(-MAX_LOG),
        );
      }
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(scan);
    };
    const observer = new MutationObserver(schedule);
    observer.observe(el, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["hidden", "role"],
    });
    schedule();
    return () => {
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [root]);
  return { problems, output };
}

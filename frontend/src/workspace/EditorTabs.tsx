import { useEffect, useRef, type KeyboardEvent } from "react";
import { create } from "zustand";
import { X } from "lucide-react";
import { useOpenFiles } from "../features/source/useOpenFiles";
import styles from "./EditorTabs.module.css";

interface PaneTabs {
  panes: string[];
  addPane: (id: string) => void;
  removePane: (id: string) => void;
}

export const usePaneTabs = create<PaneTabs>((set, get) => ({
  panes: [],
  addPane: (id) => {
    if (!get().panes.includes(id)) set({ panes: [...get().panes, id] });
  },
  removePane: (id) => set({ panes: get().panes.filter((p) => p !== id) }),
}));

interface Props {
  active: string;
  labels: Record<string, string>;
}

type Tab =
  | { key: string; kind: "pane"; id: string; label: string }
  | { key: string; kind: "file"; fileId: string; label: string; title: string };

const SOURCE = "source-title";

function go(id: string) {
  window.location.assign(`#${id}`);
}

/** Open editors: the views visited so far plus the files opened from the explorer. */
export function EditorTabs({ active, labels }: Props) {
  const { panes, addPane, removePane } = usePaneTabs();
  const files = useOpenFiles((s) => s.files);
  const activeFile = useOpenFiles((s) => s.activeId);
  const openFile = useOpenFiles((s) => s.open);
  const closeFile = useOpenFiles((s) => s.close);
  const refs = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    if (active !== SOURCE && active in labels) addPane(active);
  }, [active, labels, addPane]);

  const tabs: Tab[] = [
    ...panes
      .filter((id) => id in labels)
      .map((id) => ({
        key: id,
        kind: "pane" as const,
        id,
        label: labels[id]!,
      })),
    ...files.map((f) => ({
      key: `file:${f.fileId}`,
      kind: "file" as const,
      fileId: f.fileId,
      label: f.path.slice(f.path.lastIndexOf("/") + 1),
      title: f.path,
    })),
  ];
  if (tabs.length === 0) return null;

  const isActive = (tab: Tab) =>
    tab.kind === "pane"
      ? tab.id === active
      : active === SOURCE && tab.fileId === activeFile;

  function select(tab: Tab) {
    if (tab.kind === "pane") go(tab.id);
    else {
      const file = files.find((f) => f.fileId === tab.fileId);
      if (file) openFile(file);
      go(SOURCE);
    }
  }
  function close(tab: Tab) {
    const index = tabs.findIndex((t) => t.key === tab.key);
    const next = tabs[index + 1] ?? tabs[index - 1];
    if (tab.kind === "pane") removePane(tab.id);
    else closeFile(tab.fileId);
    if (!isActive(tab)) return;
    if (!next) go("workspace-home");
    else if (next.kind === "pane") go(next.id);
    else {
      const file = files.find((f) => f.fileId === next.fileId);
      if (file) openFile(file);
      go(SOURCE);
    }
  }
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = tabs.findIndex((t) => isActive(t));
    const at = Math.max(0, index);
    const focus = (i: number) => {
      const tab = tabs[(i + tabs.length) % tabs.length]!;
      refs.current.get(tab.key)?.focus();
    };
    if (event.key === "ArrowRight") focus(at + 1);
    else if (event.key === "ArrowLeft") focus(at - 1);
    else if (event.key === "Home") focus(0);
    else if (event.key === "End") focus(tabs.length - 1);
    else if ((event.key === "Delete" || event.key === "Backspace") && tabs[at])
      close(tabs[at]!);
    else return;
    event.preventDefault();
  }

  return (
    <div
      role="tablist"
      aria-label="Open editors"
      className={styles.strip}
      onKeyDown={onKeyDown}
    >
      {tabs.map((tab) => {
        const selected = isActive(tab);
        return (
          <div
            key={tab.key}
            role="presentation"
            className={styles.tab}
            data-active={selected || undefined}
          >
            <button
              type="button"
              role="tab"
              ref={(el) => {
                if (el) refs.current.set(tab.key, el);
                else refs.current.delete(tab.key);
              }}
              aria-selected={selected}
              aria-controls="workspace-content"
              aria-keyshortcuts="Delete"
              tabIndex={selected ? 0 : -1}
              title={`${tab.kind === "file" ? tab.title : tab.label} (Delete closes this tab)`}
              className={styles.label}
              onClick={() => select(tab)}
              onAuxClick={(event) => {
                if (event.button === 1) close(tab);
              }}
            >
              <span>{tab.label}</span>
              {/* Mouse affordance only: a tablist may own tabs and nothing else, so keyboard
                  and screen reader users close a tab with Delete or Backspace. */}
              <span
                aria-hidden="true"
                data-close=""
                className={styles.close}
                onClick={(event) => {
                  event.stopPropagation();
                  close(tab);
                }}
              >
                <X size={12} aria-hidden="true" />
              </span>
            </button>
          </div>
        );
      })}
    </div>
  );
}

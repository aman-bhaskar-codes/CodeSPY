import { useRef, type KeyboardEvent } from "react";
import { X } from "lucide-react";
import { Splitter } from "./Splitter";
import { PANEL_MAX, PANEL_MIN, type PanelTab } from "./useWorkbenchLayout";
import type { LogEntry } from "./useWorkbenchObserver";
import styles from "./BottomPanel.module.css";

interface Props {
  height: number;
  tab: PanelTab;
  problems: readonly string[];
  output: readonly LogEntry[];
  onTab: (tab: PanelTab) => void;
  onHeight: (height: number) => void;
  onResetHeight: () => void;
  onClose: () => void;
}

const TABS: { id: PanelTab; label: string }[] = [
  { id: "problems", label: "Problems" },
  { id: "output", label: "Output" },
];

/** Bottom panel: Problems (alerts shown anywhere in the editor) and Output (status messages). */
export function BottomPanel({
  height,
  tab,
  problems,
  output,
  onTab,
  onHeight,
  onResetHeight,
  onClose,
}: Props) {
  const refs = useRef(new Map<PanelTab, HTMLButtonElement>());
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = TABS.findIndex((t) => t.id === tab);
    const go = (i: number) => {
      const next = TABS[(i + TABS.length) % TABS.length]!;
      onTab(next.id);
      refs.current.get(next.id)?.focus();
    };
    if (event.key === "ArrowRight") go(index + 1);
    else if (event.key === "ArrowLeft") go(index - 1);
    else return;
    event.preventDefault();
  }
  return (
    <section
      className={styles.panel}
      style={{ height }}
      aria-label="Panel"
      data-part="panel"
      tabIndex={-1}
    >
      <Splitter
        orientation="horizontal"
        label="Resize panel"
        value={height}
        min={PANEL_MIN}
        max={PANEL_MAX}
        step={16}
        onChange={onHeight}
        onReset={onResetHeight}
      />
      <div className={styles.header}>
        <div role="tablist" aria-label="Panel views" onKeyDown={onKeyDown}>
          {TABS.map((t) => (
            <button
              key={t.id}
              ref={(el) => {
                if (el) refs.current.set(t.id, el);
              }}
              type="button"
              role="tab"
              id={`panel-tab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls="panel-body"
              tabIndex={tab === t.id ? 0 : -1}
              className={styles.tab}
              onClick={() => onTab(t.id)}
            >
              {t.label}
              {t.id === "problems" && problems.length > 0 && (
                <span className={styles.count}> {problems.length}</span>
              )}
            </button>
          ))}
        </div>
        <button
          type="button"
          className={styles.close}
          aria-label="Close panel"
          onClick={onClose}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      <div
        id="panel-body"
        role="tabpanel"
        aria-labelledby={`panel-tab-${tab}`}
        tabIndex={0}
        className={styles.body}
      >
        {tab === "problems" ? (
          problems.length === 0 ? (
            <p className={styles.empty}>No problems have been detected.</p>
          ) : (
            <ul aria-label="Problems">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )
        ) : output.length === 0 ? (
          <p className={styles.empty}>No output yet.</p>
        ) : (
          <ol aria-label="Output">
            {output.map((entry, i) => (
              <li key={`${entry.at}-${i}`}>
                <time>{entry.at}</time> {entry.text}
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

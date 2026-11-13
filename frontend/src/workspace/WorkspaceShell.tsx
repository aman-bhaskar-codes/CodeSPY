import type React from "react";
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Braces,
  FileCode2,
  Files,
  GitBranch,
  LayoutDashboard,
  ListChecks,
  Search,
  Upload,
  Workflow,
} from "lucide-react";
import { ThemeProvider, ThemeToggle } from "../theme/ThemeProvider";
import { StatusBadge } from "../ui/StatusBadge";
import { Button } from "../ui/Button";
import styles from "./WorkspaceShell.module.css";
import { Splitter } from "./Splitter";
import {
  SIDEBAR_MAX,
  SIDEBAR_MIN,
  SIDEBAR_STEP,
  useWorkbenchLayout,
} from "./useWorkbenchLayout";
import { useWorkbenchKeys } from "./useWorkbenchKeys";

export interface WorkspaceDestination {
  id: string;
  label: string;
  enabled: boolean;
}
interface Props {
  children: ReactNode;
  sessionState: "checking" | "available" | "unavailable";
  destinations: readonly WorkspaceDestination[];
  repositoryLabel?: string;
  snapshotId?: string | null;
  onSignOut?: () => void;
  signingOut?: boolean;
}
const icons = {
  "workspace-home": LayoutDashboard,
  "upload-title": Upload,
  "inventory-title": Files,
  "structural-title": GitBranch,
  "findings-title": ListChecks,
  "retrieval-title": Search,
  "workbench-title": Workflow,
};
const WorkspaceView = createContext("workspace-home");

export function WorkspacePane({
  id,
  children,
  overview = false,
}: {
  id: string;
  children: ReactNode;
  overview?: boolean;
}) {
  const active = useContext(WorkspaceView);
  const isKnownView = active in icons;
  return (
    <div
      hidden={
        isKnownView &&
        active !== id &&
        !(active === "workspace-home" && overview)
      }
    >
      {children}
    </div>
  );
}

export function WorkspaceHeading() {
  const active = useContext(WorkspaceView);
  const headings: Record<string, [string, string]> = {
    "workspace-home": [
      "Workspace overview",
      "Add a codebase, inspect its source and follow the evidence through analysis and investigation.",
    ],
    "upload-title": [
      "Add a codebase",
      "Upload an archive or import from an authorized GitHub repository.",
    ],
    "inventory-title": [
      "Source files",
      "Inspect admitted paths, content identities and analysis eligibility.",
    ],
    "structural-title": [
      "Architecture",
      "Explore syntax, symbols and supported structural relationships.",
    ],
    "findings-title": [
      "Findings",
      "Review scanner observations, source support and human decisions.",
    ],
    "retrieval-title": [
      "Search & memory",
      "Build source indexes, inspect context and review repository knowledge.",
    ],
    "workbench-title": [
      "Investigations",
      "Ask a bounded question, inspect evidence and recover saved work.",
    ],
  };
  const [title, description] = headings[active] ?? headings["workspace-home"];
  return (
    <div className="intro">
      <p className="eyebrow">Codebase workspace</p>
      <h1 id="workspace-home" tabIndex={-1}>
        {title}
      </h1>
      <p>{description}</p>
    </div>
  );
}

export function WorkspaceShell(props: Props) {
  return (
    <ThemeProvider>
      <ShellContent {...props} />
    </ThemeProvider>
  );
}

function ShellContent({
  children,
  sessionState,
  destinations,
  repositoryLabel,
  snapshotId,
  onSignOut,
  signingOut,
}: Props) {
  const [active, setActive] = useState(() =>
    window.location.hash.slice(1) in icons
      ? window.location.hash.slice(1)
      : "workspace-home",
  );
  useEffect(() => {
    const update = () => {
      const id = window.location.hash.slice(1) || "workspace-home";
      if (!(id in icons)) return;
      setActive(id);
      if (id in icons)
        requestAnimationFrame(() => document.getElementById(id)?.focus());
    };
    window.addEventListener("hashchange", update);
    window.addEventListener("popstate", update);
    return () => {
      window.removeEventListener("hashchange", update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  const sessionLabel = {
    checking: "Checking session",
    available: "Session active",
    unavailable: "Sign-in required",
  }[sessionState];
  const rootRef = useRef<HTMLDivElement>(null);
  const layout = useWorkbenchLayout();
  useWorkbenchKeys(rootRef, layout.toggleSidebar, layout.sidebarVisible);
  const activeLabel = destinations.find((d) => d.id === active)?.label;
  return (
    <WorkspaceView.Provider value={active}>
      <div
        ref={rootRef}
        className={styles.shell}
        data-workbench=""
        data-sidebar={layout.sidebarVisible ? "open" : "closed"}
        style={
          {
            "--workbench-sidebar": `${layout.sidebarWidth}px`,
          } as React.CSSProperties
        }
      >
        <a className={styles.skip} href="#workspace-content">
          Skip to workspace
        </a>
        <header className={styles.header}>
          <a
            href="#workspace-home"
            className={styles.brand}
            aria-label="CodeSPY home"
          >
            <span className={styles.mark}>
              <Braces size={16} aria-hidden="true" />
            </span>
            <span>CodeSPY</span>
          </a>
          <span className={styles.productLabel}>
            {repositoryLabel ? `${repositoryLabel} · ` : ""}
            Source workspace
          </span>
          <div className={styles.controls}>
            <StatusBadge
              tone={sessionState === "available" ? "success" : "neutral"}
            >
              {sessionLabel}
            </StatusBadge>
            <ThemeToggle />
            {onSignOut && (
              <Button pending={signingOut} onClick={onSignOut}>
                Sign out
              </Button>
            )}
          </div>
        </header>
        <nav
          className={styles.activityBar}
          aria-label="Workspace sections"
          data-part="activity"
        >
          {destinations.map((item) => {
            const Icon = icons[item.id as keyof typeof icons] ?? FileCode2;
            const content = (
              <>
                <Icon size={22} aria-hidden="true" />
                <span className={styles.label}>{item.label}</span>
              </>
            );
            return item.enabled ? (
              <a
                key={item.id}
                href={`#${item.id}`}
                title={item.label}
                aria-current={active === item.id ? "location" : undefined}
              >
                {content}
              </a>
            ) : (
              <span
                key={item.id}
                className={styles.unavailable}
                title={item.label}
                aria-disabled="true"
                aria-describedby="workspace-access-hint"
              >
                {content}
              </span>
            );
          })}
        </nav>
        <aside
          className={styles.sidebar}
          aria-label="Explorer"
          data-part="sidebar"
          tabIndex={-1}
        >
          <p className={styles.caption}>Explorer</p>
          <div className={styles.context}>
            <strong className={styles.repository}>
              {repositoryLabel || "No repository selected"}
            </strong>
            <span className={styles.snapshot}>
              {snapshotId
                ? `Snapshot ${snapshotId.slice(0, 8)}`
                : "No snapshot selected"}
            </span>
          </div>
          {destinations.some((item) => !item.enabled) && (
            <p id="workspace-access-hint" className={styles.hint}>
              {sessionState === "available"
                ? "Add a codebase to open the analysis views."
                : "Sign in to add a codebase and open its analysis views."}
            </p>
          )}
          <div className={styles.sidebarFoot}>
            <FileCode2 size={14} aria-hidden="true" /> Evidence starts with
            source.
          </div>
          <Splitter
            label="Resize explorer"
            value={layout.sidebarWidth}
            min={SIDEBAR_MIN}
            max={SIDEBAR_MAX}
            step={SIDEBAR_STEP}
            onChange={layout.setSidebarWidth}
            onReset={layout.resetSidebarWidth}
          />
        </aside>
        <main
          id="workspace-content"
          tabIndex={-1}
          className={styles.main}
          data-part="editor"
        >
          {children}
        </main>
        <footer className={styles.footer} data-part="status" tabIndex={-1}>
          <span className={styles.statusItems}>
            <span>{sessionLabel}</span>
            {repositoryLabel && <span>{repositoryLabel}</span>}
            {snapshotId && <span>Snapshot {snapshotId.slice(0, 8)}</span>}
            {activeLabel && <span>{activeLabel}</span>}
          </span>
          <span>
            <span className={styles.evidenceDot} aria-hidden="true" /> Source
            files are read-only
          </span>
        </footer>
      </div>
    </WorkspaceView.Provider>
  );
}

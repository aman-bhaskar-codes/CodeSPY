import {
  createContext,
  useContext,
  useEffect,
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
  return (
    <WorkspaceView.Provider value={active}>
      <div className={styles.shell}>
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
              <Braces size={22} aria-hidden="true" />
            </span>
            <span>CodeSPY</span>
          </a>
          <span className={styles.productLabel}>Source workspace</span>
          <div className={styles.controls}>
            <StatusBadge
              tone={sessionState === "available" ? "success" : "neutral"}
            >
              {sessionLabel}
            </StatusBadge>
            <ThemeToggle />
          {onSignOut && <Button pending={signingOut} onClick={onSignOut}>Sign out</Button>}
          </div>
        </header>
        <aside className={styles.sidebar}>
          <div className={styles.context}>
            <p className={styles.caption}>Workspace</p>
            <strong className={styles.repository}>
              {repositoryLabel || "No repository selected"}
            </strong>
            <span className={styles.snapshot}>
              {snapshotId
                ? `Snapshot ${snapshotId.slice(0, 8)}`
                : "No snapshot selected"}
            </span>
          </div>
          <nav className={styles.navigation} aria-label="Workspace sections">
            {destinations.map((item) => {
              const Icon = icons[item.id as keyof typeof icons] ?? FileCode2;
              const content = (
                <>
                  <Icon size={18} aria-hidden="true" />
                  <span>{item.label}</span>
                </>
              );
              return item.enabled ? (
                <a
                  key={item.id}
                  href={`#${item.id}`}
                  aria-current={active === item.id ? "location" : undefined}
                >
                  {content}
                </a>
              ) : (
                <span
                  key={item.id}
                  className={styles.unavailable}
                  aria-disabled="true"
                  aria-describedby="workspace-access-hint"
                >
                  {content}
                </span>
              );
            })}
          </nav>
          {destinations.some((item) => !item.enabled) && (
            <p id="workspace-access-hint" className={styles.hint}>
              {sessionState === "available"
                ? "Add a codebase to open the analysis views."
                : "Sign in to add a codebase and open its analysis views."}
            </p>
          )}
          <div className={styles.sidebarFoot}>
            <FileCode2 size={16} aria-hidden="true" /> Evidence starts with
            source.
          </div>
        </aside>
        <main id="workspace-content" tabIndex={-1} className={styles.main}>
          {children}
        </main>
        <footer className={styles.footer}>
          <span>
            <span className={styles.evidenceDot} aria-hidden="true" /> Source
            files are read-only
          </span>
          <span>CodeSPY · Source analysis workspace</span>
        </footer>
      </div>
    </WorkspaceView.Provider>
  );
}

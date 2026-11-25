import type { ComponentType } from "react";
import type { WorkspaceDestination } from "../WorkspaceShell";
import { WorkspacePane } from "../WorkspaceShell";
import type { WorkspaceController } from "../useWorkspaceController";
import { useWorkspace } from "../workspaceContext";
import { FindingsPane } from "./FindingsPane";
import { IntakePane } from "./IntakePane";
import { InventoryPane } from "./InventoryPane";
import { InvestigationsPane } from "./InvestigationsPane";
import { JobStatusPane } from "./JobStatusPane";
import { RetrievalPane } from "./RetrievalPane";
import { SourcePane } from "./SourcePane";
import { StructuralPane } from "./StructuralPane";
import type { SnapshotPaneProps } from "./types";

interface Common {
  /** Unique within the registry. */
  key: string;
  /** The hash-routed view this pane belongs to. Several panes may share one view. */
  paneId: string;
  /** Also shown on the overview. */
  overview?: boolean;
  /** Whether the pane is mounted for the current workspace state. */
  visible: (ws: WorkspaceController) => boolean;
  /** The activity bar and palette entry that opens this view; omitted for views reached another way. */
  destination?: {
    label: string;
    enabled: (ws: WorkspaceController) => boolean;
  };
}
type PaneDefinition =
  | (Common & { scope: "session"; Component: ComponentType })
  | (Common & {
      scope: "snapshot";
      Component: ComponentType<SnapshotPaneProps>;
    });

const signedIn = (ws: WorkspaceController) => !!ws.session.data;
const hasSnapshot = (ws: WorkspaceController) =>
  signedIn(ws) && !!ws.snapshot && !!ws.selection.repository;
const selected = (ws: WorkspaceController) =>
  !!ws.snapshot && !!ws.selection.repository;

/**
 * Every pane of the workspace, in render order. Adding a pane means adding one entry here;
 * the activity bar, the command palette and the view routing are derived from it.
 */
export const PANES: readonly PaneDefinition[] = [
  {
    key: "intake",
    paneId: "upload-title",
    overview: true,
    scope: "session",
    Component: IntakePane,
    visible: signedIn,
    destination: { label: "Add codebase", enabled: signedIn },
  },
  {
    key: "job",
    paneId: "upload-title",
    overview: true,
    scope: "session",
    Component: JobStatusPane,
    visible: signedIn,
  },
  {
    key: "inventory",
    paneId: "inventory-title",
    scope: "snapshot",
    Component: InventoryPane,
    visible: signedIn,
    destination: {
      label: "Files",
      enabled: (ws) => signedIn(ws) && !!ws.snapshot,
    },
  },
  {
    key: "structure",
    paneId: "structural-title",
    scope: "snapshot",
    Component: StructuralPane,
    visible: hasSnapshot,
    destination: { label: "Structure", enabled: selected },
  },
  {
    key: "findings",
    paneId: "findings-title",
    scope: "snapshot",
    Component: FindingsPane,
    visible: hasSnapshot,
    destination: { label: "Findings", enabled: selected },
  },
  {
    key: "source",
    paneId: "source-title",
    scope: "snapshot",
    Component: SourcePane,
    visible: hasSnapshot,
  },
  {
    key: "retrieval",
    paneId: "retrieval-title",
    scope: "snapshot",
    Component: RetrievalPane,
    visible: hasSnapshot,
    destination: { label: "Search & memory", enabled: selected },
  },
  {
    key: "investigations",
    paneId: "workbench-title",
    scope: "snapshot",
    Component: InvestigationsPane,
    visible: hasSnapshot,
    destination: { label: "Investigations", enabled: selected },
  },
];

/** The views the activity bar and the command palette offer, in order. */
export function workspaceDestinations(
  ws: WorkspaceController,
): WorkspaceDestination[] {
  return [
    { id: "workspace-home", label: "Overview", enabled: true },
    ...PANES.flatMap((pane) =>
      pane.destination
        ? [
            {
              id: pane.paneId,
              label: pane.destination.label,
              enabled: pane.destination.enabled(ws),
            },
          ]
        : [],
    ),
  ];
}

/** Mounts every pane that is visible for the current workspace state. */
export function PaneRegistry() {
  const ws = useWorkspace();
  const { repository } = ws.selection;
  const { snapshot } = ws;
  return (
    <>
      {PANES.filter((pane) => pane.visible(ws)).map((pane) => (
        <WorkspacePane key={pane.key} id={pane.paneId} overview={pane.overview}>
          {pane.scope === "snapshot" ? (
            <pane.Component repository={repository} snapshot={snapshot ?? ""} />
          ) : (
            <pane.Component />
          )}
        </WorkspacePane>
      ))}
    </>
  );
}

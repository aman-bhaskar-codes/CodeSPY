import { SessionBoundary } from "./workspace/SessionBoundary";
import { FileTree } from "./features/explorer/FileTree";
import { WorkspaceHeading, WorkspaceShell } from "./workspace/WorkspaceShell";
import {
  PaneRegistry,
  workspaceDestinations,
} from "./workspace/panes/registry";
import { SessionNotices } from "./workspace/panes/SessionNotices";
import { useWorkspaceController } from "./workspace/useWorkspaceController";
import { WorkspaceContext } from "./workspace/workspaceContext";

export default function App() {
  return (
    <SessionBoundary>
      <WorkspaceApp />
    </SessionBoundary>
  );
}

function WorkspaceApp() {
  const ws = useWorkspaceController();
  const { session, logout, repositoryItems, snapshot } = ws;
  const { repository } = ws.selection;
  return (
    <WorkspaceContext value={ws}>
      <WorkspaceShell
        onSignOut={session.data ? () => logout.mutate() : undefined}
        signingOut={logout.isPending}
        sessionState={
          session.isPending
            ? "checking"
            : session.data
              ? "available"
              : "unavailable"
        }
        repositoryLabel={
          repositoryItems.find((item) => item.id === repository)?.display_name
        }
        snapshotId={snapshot}
        explorer={
          session.data && snapshot && repository ? (
            <FileTree repositoryId={repository} snapshotId={snapshot} />
          ) : undefined
        }
        destinations={workspaceDestinations(ws)}
      >
        <WorkspaceHeading />
        <SessionNotices />
        {session.data && <PaneRegistry />}
      </WorkspaceShell>
    </WorkspaceContext>
  );
}

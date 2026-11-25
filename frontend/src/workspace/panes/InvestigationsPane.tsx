// Moved verbatim from the workspace composition; behaviour is unchanged.
import { InvestigationsWorkbench } from "../../features/investigations/InvestigationsWorkbench";
import { useWorkspace } from "../workspaceContext";
import type { SnapshotPaneProps } from "./types";
export function InvestigationsPane({
  repository,
  snapshot,
}: SnapshotPaneProps) {
  const { session, selection } = useWorkspace();
  const { findingTarget } = selection;
  return (
    <>
      <InvestigationsWorkbench
        findingId={
          findingTarget?.repositoryId === repository &&
          findingTarget?.snapshotId === snapshot
            ? findingTarget.findingId
            : undefined
        }
        findingRunId={
          findingTarget?.repositoryId === repository &&
          findingTarget?.snapshotId === snapshot
            ? findingTarget.runId
            : undefined
        }
        key={`${repository}:${snapshot}`}
        repositoryId={repository}
        snapshotId={snapshot}
        csrfToken={session.data?.csrf_token ?? ""}
      />
    </>
  );
}

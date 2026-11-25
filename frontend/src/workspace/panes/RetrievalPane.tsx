// Moved verbatim from the workspace composition; behaviour is unchanged.
import { RetrievalWorkbench } from "../../features/retrieval/RetrievalWorkbench";
import { useWorkspace } from "../workspaceContext";
import type { SnapshotPaneProps } from "./types";
export function RetrievalPane({ repository, snapshot }: SnapshotPaneProps) {
  const { session } = useWorkspace();
  return (
    <>
      <RetrievalWorkbench
        repositoryId={repository}
        snapshotId={snapshot}
        csrfToken={session.data?.csrf_token ?? ""}
      />
    </>
  );
}

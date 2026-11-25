// Moved verbatim from the workspace composition; behaviour is unchanged.
import { SourceEditor } from "../../features/source/SourceEditor";
import type { SnapshotPaneProps } from "./types";
export function SourcePane({ repository, snapshot }: SnapshotPaneProps) {
  return (
    <>
      <SourceEditor repositoryId={repository} snapshotId={snapshot} />
    </>
  );
}

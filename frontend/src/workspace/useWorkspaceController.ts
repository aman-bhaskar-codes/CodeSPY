import { useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { WorkspaceDeps } from "./controllerDeps";
import { useAnalysis } from "./useAnalysis";
import { useIntake } from "./useIntake";
import { useInventory } from "./useInventory";
import { useJobControl } from "./useJobControl";
import { useJobStatus } from "./useJobStatus";
import { useScan } from "./useScan";
import { useRepositories, useSessionData } from "./useSessionData";
import { useSnapshotUrl, useWorkspaceSelection } from "./useWorkspaceSelection";

/**
 * Everything the workspace panes share: the URL-backed selection, the session, the queries that
 * follow the selected work, and the commands that change it. Panes read this through context.
 */
export function useWorkspaceController() {
  const client = useQueryClient();
  const selection = useWorkspaceSelection();
  const retryKeys = useRef(new Map<string, string>());
  const { session, logout } = useSessionData();
  const { repositories, selectedMetadata, repositoryItems } = useRepositories(
    selection.repository,
    session,
  );
  const { submission, job, selectedJob, snapshot } = useJobStatus(
    session,
    selection.submissionId,
    selection.jobId,
    selection.restoredSnapshot,
  );
  useSnapshotUrl(snapshot, selection.locationSearchRef);
  const inventory = useInventory(
    session,
    selection.repository,
    snapshot,
    selection.pages,
  );
  const deps: WorkspaceDeps = { client, session, selection, retryKeys };
  const analysis = useAnalysis(deps, snapshot);
  const scan = useScan(deps, snapshot);
  const intake = useIntake(deps, () => repositories.refetch());
  const { action, cancelButtonRef } = useJobControl(deps, selectedJob);
  const busy =
    intake.creatingRepository ||
    intake.upload.isPending ||
    action.isPending ||
    analysis.analyze.isPending ||
    scan.startScan.isPending ||
    intake.acquire.isPending ||
    scan.retryScan.isPending ||
    scan.cancelScan.isPending ||
    analysis.retryAnalysis.isPending ||
    analysis.cancelAnalysis.isPending;
  return {
    client,
    selection,
    session,
    logout,
    repositories,
    selectedMetadata,
    repositoryItems,
    submission,
    job,
    selectedJob,
    snapshot,
    inventory,
    analysis,
    scan,
    intake,
    action,
    cancelButtonRef,
    busy,
  };
}

export type WorkspaceController = ReturnType<typeof useWorkspaceController>;

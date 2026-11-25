import { useEffect, useRef, useState } from "react";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function selectedId(name: string): string | null {
  const value = new URLSearchParams(window.location.search).get(name);
  return value && uuid.test(value) ? value : null;
}

export function findingTargetFromUrl() {
  const repositoryId = selectedId("repository"),
    snapshotId = selectedId("snapshot"),
    findingId = selectedId("finding"),
    runId = selectedId("finding_run");
  return repositoryId && snapshotId && findingId && runId
    ? { repositoryId, snapshotId, findingId, runId }
    : null;
}

/**
 * The workspace selection: which repository, submission, job, scan, snapshot
 * and finding the page shows, restored from the URL on load and on history
 * navigation, and written back to the URL as it changes.
 */
export function useWorkspaceSelection() {
  const [findingTarget, setFindingTarget] = useState(findingTargetFromUrl);
  const [repository, setRepository] = useState(
    () => selectedId("repository") ?? "",
  );
  const [submissionId, setSubmissionId] = useState<string | null>(() =>
    selectedId("submission"),
  );
  const [jobId, setJobId] = useState<string | null>(() => selectedId("job"));
  const [restoredSnapshot, setRestoredSnapshot] = useState<string | null>(() =>
    selectedId("snapshot"),
  );
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [pages, setPages] = useState<(string | null)[]>([null]);
  const [analysisJobId, setAnalysisJobId] = useState<string | null>(null);
  const [scanJobId, setScanJobId] = useState<string | null>(() =>
    selectedId("scan"),
  );
  const locationSearchRef = useRef(window.location.search);
  const navigationRevisionRef = useRef(0);
  const responseIsCurrent = () => {
    const revision = navigationRevisionRef.current;
    return () => revision === navigationRevisionRef.current;
  };
  useEffect(() => {
    const restore = () => {
      if (locationSearchRef.current === window.location.search) return;
      navigationRevisionRef.current += 1;
      locationSearchRef.current = window.location.search;
      setRepository(selectedId("repository") ?? "");
      setSubmissionId(selectedId("submission"));
      setJobId(selectedId("job"));
      setScanJobId(selectedId("scan"));
      setRestoredSnapshot(selectedId("snapshot"));
      setFindingTarget(findingTargetFromUrl());
      setAnalysisJobId(null);
      setPages([null]);
      setConfirmCancel(false);
    };
    window.addEventListener("popstate", restore);
    return () => {
      navigationRevisionRef.current += 1;
      window.removeEventListener("popstate", restore);
    };
  }, []);
  useEffect(() => {
    const url = new URL(window.location.href);
    for (const [name, value] of [
      ["repository", repository],
      ["submission", submissionId],
      ["job", jobId],
      ["scan", scanJobId],
    ]) {
      if (value) url.searchParams.set(name!, value);
      else url.searchParams.delete(name!);
    }
    window.history.replaceState(window.history.state, "", url);
    locationSearchRef.current = url.search;
  }, [repository, submissionId, jobId, scanJobId]);
  return {
    findingTarget,
    setFindingTarget,
    repository,
    setRepository,
    submissionId,
    setSubmissionId,
    jobId,
    setJobId,
    restoredSnapshot,
    setRestoredSnapshot,
    confirmCancel,
    setConfirmCancel,
    pages,
    setPages,
    analysisJobId,
    setAnalysisJobId,
    scanJobId,
    setScanJobId,
    locationSearchRef,
    navigationRevisionRef,
    responseIsCurrent,
  };
}

/** Mirrors the resolved snapshot into the URL. */
export function useSnapshotUrl(
  snapshot: string | null | undefined,
  locationSearchRef: { current: string },
) {
  useEffect(() => {
    const url = new URL(window.location.href);
    if (snapshot) url.searchParams.set("snapshot", snapshot);
    else url.searchParams.delete("snapshot");
    window.history.replaceState(window.history.state, "", url);
    locationSearchRef.current = url.search;
  }, [snapshot, locationSearchRef]);
}

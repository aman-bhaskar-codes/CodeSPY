import { SessionBoundary } from "./workspace/SessionBoundary";
import { GitHubImportForm } from "./features/intake/GitHubImportForm";
import { RepositoryCreateForm } from "./features/intake/RepositoryCreateForm";
import { useEffect, useRef, useState } from "react";
import {
  WorkspaceHeading,
  WorkspacePane,
  WorkspaceShell,
} from "./workspace/WorkspaceShell";
import { Panel } from "./ui/Panel";
import { Button } from "./ui/Button";
import {
  useMutation,
  useQuery,
  useQueryClient,
  useInfiniteQuery,
  type InfiniteData,
} from "@tanstack/react-query";
import { api, ApiError, mutationHeaders, request } from "./api/client";
import type {
  SubmissionRecordV1,
  RepositoryPageV1,
  RetryResponseV1,
  RetryResponseV2,
} from "./api/generated/types.gen";
import { StructuralReportView } from "./features/structure/StructuralReportView";
import { FindingsWorkbench } from "./features/findings/FindingsWorkbench";
import { RetrievalWorkbench } from "./features/retrieval/RetrievalWorkbench";
import { InvestigationsWorkbench } from "./features/investigations/InvestigationsWorkbench";

const terminal = new Set(["succeeded", "partial", "failed", "cancelled"]);
function selectedId(name: string): string | null {
  const value = new URLSearchParams(window.location.search).get(name);
  return value &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      value,
    )
    ? value
    : null;
}
function findingTargetFromUrl() {
  const repositoryId = selectedId("repository"),
    snapshotId = selectedId("snapshot"),
    findingId = selectedId("finding"),
    runId = selectedId("finding_run");
  return repositoryId && snapshotId && findingId && runId
    ? { repositoryId, snapshotId, findingId, runId }
    : null;
}
const label = (value: string) => value.replaceAll("_", " ");
function problem(error: unknown): string {
  if (error instanceof ApiError && error.status === 401)
    return "Your session expired. Sign in again before resubmitting sources.";
  if (error instanceof ApiError && error.status === 403)
    return "Access changed. Reload your session and repository list.";
  if (error instanceof ApiError && error.code === "analysis_unavailable")
    return "Structural analysis is unavailable. Start the qualified local runtime before retrying.";
  if (error instanceof ApiError && error.code === "scan_unavailable")
    return "Scanner analysis is unavailable. Start the qualified local runtime before retrying.";
  if (error instanceof ApiError && error.status === 409)
    return "This request conflicts with existing work. Reload its status before retrying.";
  if (error instanceof ApiError && error.status === 429)
    return "The work limit has been reached. Wait for current work to finish.";
  return "The request could not be confirmed. Reload its status before starting new work.";
}

export default function App() {
  return (
    <SessionBoundary>
      <WorkspaceApp />
    </SessionBoundary>
  );
}

function WorkspaceApp() {
  const client = useQueryClient();
  const [findingTarget, setFindingTarget] = useState(findingTargetFromUrl);
  const [creatingRepository, setCreatingRepository] = useState(false);
  const [repository, setRepository] = useState(
    () => selectedId("repository") ?? "",
  );
  const [file, setFile] = useState<File | null>(null);
  const [submissionId, setSubmissionId] = useState<string | null>(() =>
    selectedId("submission"),
  );
  const [jobId, setJobId] = useState<string | null>(() => selectedId("job"));
  const [restoredSnapshot, setRestoredSnapshot] = useState<string | null>(() =>
    selectedId("snapshot"),
  );
  const [validation, setValidation] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [pages, setPages] = useState<(string | null)[]>([null]);
  const [analysisJobId, setAnalysisJobId] = useState<string | null>(null);
  const [scanJobId, setScanJobId] = useState<string | null>(() =>
    selectedId("scan"),
  );
  const input = useRef<HTMLInputElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const uploadKey = useRef(crypto.randomUUID());
  const retryKeys = useRef(new Map<string, string>());
  const locationSearch = useRef(window.location.search);
  const navigationRevision = useRef(0);
  const responseIsCurrent = () => {
    const revision = navigationRevision.current;
    return () => revision === navigationRevision.current;
  };
  useEffect(() => {
    const restore = () => {
      if (locationSearch.current === window.location.search) return;
      navigationRevision.current += 1;
      locationSearch.current = window.location.search;
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
      navigationRevision.current += 1;
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
    locationSearch.current = url.search;
  }, [repository, submissionId, jobId, scanJobId]);
  const session = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => api.session(signal),
    retry: false,
  });
  const logout = useMutation({
    mutationFn: () => api.logout(session.data!.csrf_token),
    onSuccess: () => window.dispatchEvent(new Event("codespy:access-required")),
  });
  const repositories = useInfiniteQuery({
    queryKey: ["repositories"],
    initialPageParam: null as string | null,
    queryFn: ({ signal, pageParam }) => api.repositories(signal, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!session.data,
  });
  const pagedRepositories =
    repositories.data?.pages.flatMap((page) => page.items) ?? [];
  const selectedMetadata = useQuery({
    queryKey: ["repository-metadata", repository],
    queryFn: ({ signal }) => api.repositoryMetadata(repository, signal),
    enabled:
      !!repository &&
      !!session.data &&
      repositories.isSuccess &&
      !pagedRepositories.some((item) => item.id === repository),
    retry: false,
  });
  const repositoryItems =
    selectedMetadata.data?.id === repository &&
    !pagedRepositories.some((item) => item.id === repository)
      ? [...pagedRepositories, selectedMetadata.data]
      : pagedRepositories;
  const submission = useQuery({
    queryKey: ["submission", submissionId],
    queryFn: ({ signal }) => api.submission(submissionId!, signal),
    enabled: !!submissionId && !!session.data,
    refetchInterval: (query) =>
      query.state.data &&
      ["admitted", "rejected", "failed", "cancelled", "expired"].includes(
        query.state.data.state,
      )
        ? false
        : 1500,
  });
  const anchorJob = jobId ?? submission.data?.job_id;
  const parentJob = useQuery({
    queryKey: ["job", anchorJob],
    queryFn: ({ signal }) => api.job(anchorJob!, signal),
    enabled: !!anchorJob && !!session.data,
    refetchInterval: (query) =>
      query.state.data && terminal.has(query.state.data.state) ? false : 1500,
  });
  const selectedJob = parentJob.data?.inventory_job_id ?? anchorJob;
  const job = useQuery({
    queryKey: ["job", selectedJob],
    queryFn: ({ signal }) => api.job(selectedJob!, signal),
    enabled: !!selectedJob && !!session.data,
    refetchInterval: (query) =>
      query.state.data && terminal.has(query.state.data.state) ? false : 2000,
  });
  const snapshot =
    submission.data?.snapshot_id ?? job.data?.snapshot_id ?? restoredSnapshot;
  useEffect(() => {
    const url = new URL(window.location.href);
    if (snapshot) url.searchParams.set("snapshot", snapshot);
    else url.searchParams.delete("snapshot");
    window.history.replaceState(window.history.state, "", url);
    locationSearch.current = url.search;
  }, [snapshot]);
  const inventory = useQuery({
    queryKey: ["inventory", repository, snapshot, pages.at(-1)],
    queryFn: ({ signal }) =>
      api.inventory(
        snapshot!,
        pages.at(-1) ?? null,
        signal,
        repository || undefined,
      ),
    enabled: !!snapshot && !!session.data,
  });
  const structuralReport = useQuery({
    queryKey: ["structuralReport", repository, snapshot],
    queryFn: ({ signal }) =>
      api.snapshotStructuralReport(repository, snapshot!, signal),
    enabled: !!snapshot && !!repository && !!session.data,
    retry: false,
  });
  const analysisJob = useQuery({
    queryKey: ["job", analysisJobId],
    queryFn: ({ signal }) => api.job(analysisJobId!, signal),
    enabled: !!analysisJobId && !!session.data,
    refetchInterval: (query) =>
      query.state.data && terminal.has(query.state.data.state) ? false : 1500,
  });
  useEffect(() => {
    if (
      analysisJob.data?.state === "succeeded" ||
      analysisJob.data?.state === "partial"
    ) {
      void client.invalidateQueries({
        queryKey: ["structuralReport", repository, snapshot],
      });
    }
  }, [analysisJob.data?.state, client, repository, snapshot]);
  const analyze = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!repository || !snapshot || !session.data)
        throw new Error("missing input");
      const res = await api.createAnalysis(
        repository,
        snapshot,
        session.data.csrf_token,
      );
      if (isCurrent()) setAnalysisJobId(res.job_id);
    },
  });
  const cancelAnalysis = useMutation({
    mutationFn: async () => {
      if (!analysisJobId || !session.data) throw new Error("missing analysis");
      await request(`/jobs/${analysisJobId}/cancel`, {
        method: "POST",
        headers: mutationHeaders(session.data.csrf_token),
      });
      await client.invalidateQueries({ queryKey: ["job", analysisJobId] });
    },
  });
  const retryAnalysis = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!analysisJobId || !session.data) throw new Error("missing analysis");
      let key = retryKeys.current.get(analysisJobId);
      if (!key) {
        key = crypto.randomUUID();
        retryKeys.current.set(analysisJobId, key);
      }
      const result = await request<RetryResponseV2>(
        `/jobs/${analysisJobId}/retry`,
        {
          method: "POST",
          headers: mutationHeaders(session.data.csrf_token, key),
        },
      );
      if (isCurrent()) setAnalysisJobId(result.retry_job_id);
    },
  });
  const scanConfiguration = useQuery({
    queryKey: ["scan-configuration", repository, snapshot],
    queryFn: ({ signal }) =>
      api.scanConfiguration(repository, snapshot!, signal),
    enabled: !!repository && !!snapshot && !!session.data,
    retry: false,
  });
  const scanJob = useQuery({
    queryKey: ["scan-job", repository, snapshot, scanJobId],
    queryFn: ({ signal }) =>
      api.scanJob(repository, snapshot!, scanJobId!, signal),
    enabled: !!repository && !!snapshot && !!scanJobId && !!session.data,
    refetchInterval: (query) =>
      terminal.has(query.state.data?.state ?? "") ? false : 1500,
  });
  const startScan = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (
        !repository ||
        !snapshot ||
        !session.data ||
        !scanConfiguration.data?.config
      )
        throw new Error("scan_configuration_unavailable");
      const key = `scan:${repository}:${snapshot}`;
      if (!retryKeys.current.has(key))
        retryKeys.current.set(key, crypto.randomUUID());
      const result = await api.createScan(
        repository,
        snapshot,
        session.data.csrf_token,
        retryKeys.current.get(key),
        scanConfiguration.data.config,
      );
      if (isCurrent()) setScanJobId(result.job_id);
      retryKeys.current.delete(key);
    },
  });
  const cancelScan = useMutation({
    mutationFn: async () => {
      if (!scanJobId || !snapshot || !session.data)
        throw new Error("missing scan");
      await api.cancelScan(
        repository,
        snapshot,
        scanJobId,
        session.data.csrf_token,
        `cancel-${scanJobId}`,
      );
      await client.invalidateQueries({
        queryKey: ["scan-job", repository, snapshot, scanJobId],
      });
    },
  });
  const retryScan = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!scanJobId || !snapshot || !session.data)
        throw new Error("missing scan");
      if (!retryKeys.current.has(scanJobId))
        retryKeys.current.set(scanJobId, crypto.randomUUID());
      const result = await api.retryScan(
        repository,
        snapshot,
        scanJobId,
        session.data.csrf_token,
        retryKeys.current.get(scanJobId)!,
      );
      if (isCurrent()) setScanJobId(result.retry_job_id);
    },
  });
  const acquire = useMutation({
    mutationFn: async (ref: string) => {
      if (!session.data) throw new Error("missing session");
      const intent = `github:${repository}:${ref}`;
      if (!retryKeys.current.has(intent))
        retryKeys.current.set(intent, crypto.randomUUID());
      const isCurrent = responseIsCurrent();
      const record = await api.acquireGitHub(
        repository,
        ref,
        session.data.csrf_token,
        retryKeys.current.get(intent)!,
      );
      return { record, isCurrent };
    },
    onSuccess: ({ record, isCurrent }) => {
      if (!isCurrent()) return;
      setSubmissionId(record.id);
      setJobId(record.job_id ?? null);
      setRestoredSnapshot(null);
      setPages([null]);
    },
  });
  const upload = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!file || !repository || !session.data)
        throw new Error("missing input");
      setRestoredSnapshot(null);
      const reserved = await request<SubmissionRecordV1>(
        `/repositories/${repository}/uploads`,
        {
          method: "POST",
          headers: mutationHeaders(session.data.csrf_token, uploadKey.current),
          body: JSON.stringify({
            repository_id: repository,
            filename: file.name,
            declared_size: file.size,
          }),
        },
      );
      if (isCurrent()) setSubmissionId(reserved.id);
      const result = await request<SubmissionRecordV1>(
        `/submissions/${reserved.id}/content`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/zip",
            "X-CSRF-Token": session.data.csrf_token,
          },
          body: file,
        },
      );
      if (isCurrent()) setJobId(result.job_id ?? null);
      await client.invalidateQueries({ queryKey: ["submission", reserved.id] });
    },
  });
  const action = useMutation({
    mutationFn: async (kind: "cancel" | "retry") => {
      const isCurrent = responseIsCurrent();
      if (!selectedJob || !session.data) throw new Error("missing job");
      const storageKey = `codespy:retry:${selectedJob}`;
      let commandKey = retryKeys.current.get(selectedJob);
      if (kind === "retry" && !commandKey) {
        try {
          commandKey = window.sessionStorage.getItem(storageKey) ?? undefined;
        } catch {
          /* private-storage fallback */
        }
        commandKey ??= crypto.randomUUID();
        retryKeys.current.set(selectedJob, commandKey);
        try {
          window.sessionStorage.setItem(storageKey, commandKey);
        } catch {
          /* retained in memory */
        }
      }
      const result = await request<RetryResponseV1 | object>(
        `/jobs/${selectedJob}/${kind}`,
        {
          method: "POST",
          headers: mutationHeaders(session.data.csrf_token, commandKey),
        },
      );
      if (isCurrent() && kind === "retry" && "retry_job_id" in result) {
        const retried = result as RetryResponseV1;
        setRestoredSnapshot(null);
        setJobId(retried.retry_job_id);
        setSubmissionId(retried.submission_id);
        setPages([null]);
      }
      setConfirmCancel(false);
      cancelButton.current?.focus();
      await client.invalidateQueries({ queryKey: ["job"] });
      await client.invalidateQueries({ queryKey: ["submission"] });
    },
  });
  const busy =
    creatingRepository ||
    upload.isPending ||
    action.isPending ||
    analyze.isPending ||
    startScan.isPending ||
    acquire.isPending ||
    retryScan.isPending ||
    cancelScan.isPending ||
    retryAnalysis.isPending ||
    cancelAnalysis.isPending;
  return (
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
      destinations={[
        { id: "workspace-home", label: "Overview", enabled: true },
        { id: "upload-title", label: "Add codebase", enabled: !!session.data },
        {
          id: "inventory-title",
          label: "Files",
          enabled: !!session.data && !!snapshot,
        },
        {
          id: "structural-title",
          label: "Structure",
          enabled: !!snapshot && !!repository,
        },
        {
          id: "findings-title",
          label: "Findings",
          enabled: !!snapshot && !!repository,
        },
        {
          id: "retrieval-title",
          label: "Search & memory",
          enabled: !!snapshot && !!repository,
        },
        {
          id: "workbench-title",
          label: "Investigations",
          enabled: !!snapshot && !!repository,
        },
      ]}
    >
      <WorkspaceHeading />
      {logout.isError && (
        <p role="alert">Sign-out could not be confirmed. Retry signing out.</p>
      )}
      {session.isPending && <p role="status">Checking your session…</p>}
      {session.isError && (
        <Panel
          id="session-unavailable"
          title="Sign-in is unavailable"
          role="alert"
        >
          <p>
            The local fixture or GitHub sign-in provider must be configured
            before you can submit sources.
          </p>
          <div className="session-actions">
            <Button onClick={() => void session.refetch()}>
              Retry session
            </Button>
            <a href="/api/v1/auth/github/start">Sign in with GitHub</a>
          </div>
        </Panel>
      )}
      {session.data && (
        <>
          <WorkspacePane id="upload-title" overview>
            <section aria-labelledby="upload-title">
              <h2 id="upload-title" tabIndex={-1}>
                Add a source archive
              </h2>
              <p>
                ZIP only, up to 100 MiB. Archive contents are inventoried
                without executing source files.
              </p>
              {repositories.isPending && (
                <p role="status">Loading repositories…</p>
              )}
              {repositories.isError && (
                <p role="alert">
                  Repositories could not be loaded.{" "}
                  <button onClick={() => void repositories.refetch()}>
                    Reload repositories
                  </button>
                </p>
              )}
              {selectedMetadata.isError && (
                <p role="alert">
                  The selected repository is unavailable. Choose another
                  authorized repository.
                </p>
              )}
              {repositories.isSuccess && repositoryItems.length === 0 && (
                <p>No repositories are available.</p>
              )}
              <RepositoryCreateForm
                disabled={(busy && !creatingRepository) || !!submissionId}
                onPendingChange={setCreatingRepository}
                csrfToken={session.data.csrf_token}
                onCreated={(record) => {
                  client.setQueryData<InfiniteData<RepositoryPageV1>>(
                    ["repositories"],
                    (current) =>
                      current
                        ? {
                            ...current,
                            pages: [
                              {
                                ...current.pages[0],
                                items: [
                                  {
                                    id: record.id,
                                    org_id: record.org_id,
                                    display_name: record.display_name,
                                    source_kind: record.source_kind,
                                  },
                                  ...(current.pages[0]?.items ?? []),
                                ],
                              },
                              ...current.pages.slice(1),
                            ],
                          }
                        : current,
                  );
                  setRepository(record.id);
                  setRestoredSnapshot(null);
                  setSubmissionId(null);
                  setJobId(null);
                  setScanJobId(null);
                  setAnalysisJobId(null);
                  setPages([null]);
                }}
              />
              <form
                noValidate
                onSubmit={(event) => {
                  event.preventDefault();
                  if (
                    !repository ||
                    !file ||
                    file.size === 0 ||
                    file.size > 104857600 ||
                    !file.name.toLowerCase().endsWith(".zip")
                  ) {
                    setValidation(
                      "Choose a repository and a nonempty ZIP no larger than 100 MiB.",
                    );
                    input.current?.focus();
                    return;
                  }
                  setValidation("");
                  upload.mutate();
                }}
              >
                <label htmlFor="repository">Repository</label>
                <select
                  id="repository"
                  value={repository}
                  disabled={busy || !!submissionId}
                  onChange={(event) => {
                    if (event.target.value === repository) return;
                    navigationRevision.current += 1;
                    setRepository(event.target.value);
                    setRestoredSnapshot(null);
                    setSubmissionId(null);
                    setJobId(null);
                    setScanJobId(null);
                    setAnalysisJobId(null);
                    setPages([null]);
                    setConfirmCancel(false);
                    const url = new URL(window.location.href);
                    url.searchParams.delete("investigation");
                    window.history.replaceState(window.history.state, "", url);
                    uploadKey.current = crypto.randomUUID();
                  }}
                >
                  <option value="">Choose a repository</option>
                  {repositoryItems.map((repo) => (
                    <option key={repo.id} value={repo.id}>
                      {repo.display_name}
                    </option>
                  ))}
                </select>
                {repositories.hasNextPage && (
                  <button
                    type="button"
                    disabled={repositories.isFetchingNextPage}
                    onClick={() => void repositories.fetchNextPage()}
                  >
                    Load more repositories
                  </button>
                )}
                <label htmlFor="archive">Source ZIP</label>
                <input
                  ref={input}
                  id="archive"
                  type="file"
                  accept=".zip,application/zip"
                  disabled={
                    busy ||
                    !!submissionId ||
                    repositoryItems.find((item) => item.id === repository)
                      ?.source_kind === "github"
                  }
                  aria-invalid={!!validation}
                  aria-describedby="upload-help"
                  onChange={(event) => {
                    setFile(event.target.files?.[0] ?? null);
                    uploadKey.current = crypto.randomUUID();
                  }}
                />
                <p id="upload-help" className={validation ? "error" : "muted"}>
                  {validation ||
                    "Paths and hashes will appear after admission. Sensitive paths remain excluded."}
                </p>
                <button
                  className="primary"
                  type="submit"
                  disabled={
                    busy ||
                    !repositoryItems.length ||
                    repositoryItems.find((item) => item.id === repository)
                      ?.source_kind === "github" ||
                    (!!submissionId && !upload.isError)
                  }
                  aria-busy={upload.isPending}
                >
                  {upload.isPending ? "Uploading…" : "Upload archive"}
                </button>
              </form>
              {repositoryItems.find((item) => item.id === repository)
                ?.source_kind === "github" && (
                <GitHubImportForm
                  pending={busy}
                  onImport={(ref) => acquire.mutate(ref)}
                />
              )}
              {acquire.isError && <p role="alert">{problem(acquire.error)}</p>}
              {upload.isError && <p role="alert">{problem(upload.error)}</p>}
            </section>
          </WorkspacePane>
          <WorkspacePane id="upload-title" overview>
            <section aria-labelledby="job-title">
              <h2 id="job-title">Current work</h2>
              <div role="status" aria-live="polite" className="status-line">
                {job.data
                  ? ` ${job.data.operation === "inventory_snapshot" ? "Inventory" : "Admission"} ${label(job.data.state)} · attempt ${job.data.attempt_count} of ${job.data.max_attempts}`
                  : submission.data
                    ? `Submission ${label(submission.data.state)}`
                    : upload.isPending
                      ? "Sending source archive…"
                      : "No source submitted in this session."}
              </div>
              {(job.isError || submission.isError) && (
                <p role="alert">
                  {problem(job.error ?? submission.error)}{" "}
                  <button
                    onClick={() => {
                      void job.refetch();
                      void submission.refetch();
                    }}
                  >
                    Reload status
                  </button>
                </p>
              )}
              {job.data?.failure_code && (
                <p>Reason: {label(job.data.failure_code)}</p>
              )}
              {selectedJob && job.data && !terminal.has(job.data.state) && (
                <button
                  ref={cancelButton}
                  disabled={busy}
                  onClick={() => setConfirmCancel(true)}
                >
                  Cancel work
                </button>
              )}
              {confirmCancel && (
                <div className="confirmation">
                  <p>
                    Cancel this job? Verified input is retained for a limited
                    time so you can request a retry.
                  </p>
                  <button
                    autoFocus
                    onClick={() => {
                      setConfirmCancel(false);
                      cancelButton.current?.focus();
                    }}
                  >
                    Keep working
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => action.mutate("cancel")}
                  >
                    Confirm cancellation
                  </button>
                </div>
              )}
              {job.data && ["failed", "cancelled"].includes(job.data.state) && (
                <button disabled={busy} onClick={() => action.mutate("retry")}>
                  Request retry
                </button>
              )}
              {action.isError && <p role="alert">{problem(action.error)}</p>}
              {((job.data && terminal.has(job.data.state)) ||
                (submission.data &&
                  ["rejected", "failed", "cancelled", "expired"].includes(
                    submission.data.state,
                  ))) && (
                <button
                  disabled={busy}
                  onClick={() => {
                    const url = new URL(window.location.href);
                    for (const key of ["submission", "job", "scan", "snapshot"])
                      url.searchParams.delete(key);
                    window.history.replaceState(window.history.state, "", url);
                    locationSearch.current = url.search;
                    setRestoredSnapshot(null);
                    setSubmissionId(null);
                    setJobId(null);
                    setScanJobId(null);
                    setFile(null);
                    setPages([null]);
                    setAnalysisJobId(null);
                    upload.reset();
                    action.reset();
                    analyze.reset();
                    uploadKey.current = crypto.randomUUID();
                    if (input.current) input.current.value = "";
                  }}
                >
                  Start another submission
                </button>
              )}
            </section>
          </WorkspacePane>
          <WorkspacePane id="inventory-title">
            <section aria-labelledby="inventory-title">
              <div className="section-heading">
                <h2 id="inventory-title" tabIndex={-1}>
                  File inventory
                </h2>
                <span className="muted">
                  Byte evidence · no semantic analysis
                </span>
              </div>
              {!snapshot && (
                <p>The inventory appears after a source is admitted.</p>
              )}
              {snapshot && inventory.isPending && (
                <p role="status">Loading inventory…</p>
              )}
              {inventory.isError && (
                <p role="alert">
                  Inventory could not be loaded.{" "}
                  <button onClick={() => void inventory.refetch()}>
                    Reload inventory
                  </button>
                </p>
              )}
              {inventory.data && (
                <>
                  <p>
                    {inventory.data.items.length} files on this page.
                    Eligibility describes admission policy, not analysis
                    coverage.
                  </p>
                  {inventory.data.items.length === 0 ? (
                    <p>This snapshot contains no regular files.</p>
                  ) : (
                    <div
                      className="table-scroll"
                      tabIndex={0}
                      role="region"
                      aria-label="File inventory table"
                    >
                      <table>
                        <thead>
                          <tr>
                            <th scope="col">Path</th>
                            <th scope="col">Bytes</th>
                            <th scope="col">SHA-256</th>
                            <th scope="col">Eligibility</th>
                          </tr>
                        </thead>
                        <tbody>
                          {inventory.data.items.map((entry) => (
                            <tr key={entry.id}>
                              <th scope="row">{entry.path}</th>
                              <td>
                                {entry.size_bytes.toLocaleString("en-US")}
                              </td>
                              <td className="hash">{entry.sha256}</td>
                              <td>
                                {entry.eligible
                                  ? "Eligible"
                                  : `Excluded: ${label(entry.exclusion_reason ?? "unspecified")}`}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  <nav aria-label="Inventory pages">
                    <button
                      disabled={pages.length < 2 || inventory.isFetching}
                      onClick={() =>
                        setPages((current) => current.slice(0, -1))
                      }
                    >
                      Previous page
                    </button>
                    <span>Page {pages.length}</span>
                    <button
                      disabled={
                        !inventory.data.next_cursor || inventory.isFetching
                      }
                      onClick={() =>
                        setPages((current) => [
                          ...current,
                          inventory.data.next_cursor ?? null,
                        ])
                      }
                    >
                      Next page
                    </button>
                  </nav>
                </>
              )}
            </section>
          </WorkspacePane>
          {snapshot && repository && (
            <WorkspacePane id="structural-title">
              <section aria-labelledby="structural-title">
                <div className="section-heading">
                  <h2 id="structural-title" tabIndex={-1}>
                    Structural Architecture
                  </h2>
                  <span className="muted">
                    Deterministic syntax & graph analysis
                  </span>
                </div>
                {analysisJob.data && !terminal.has(analysisJob.data.state) && (
                  <button
                    type="button"
                    disabled={cancelAnalysis.isPending}
                    onClick={() => cancelAnalysis.mutate()}
                  >
                    Cancel structural analysis
                  </button>
                )}
                {analysisJob.data &&
                  ["failed", "cancelled"].includes(analysisJob.data.state) && (
                    <button
                      type="button"
                      disabled={retryAnalysis.isPending}
                      onClick={() => retryAnalysis.mutate()}
                    >
                      Retry structural analysis
                    </button>
                  )}
                {(cancelAnalysis.isError || retryAnalysis.isError) && (
                  <p role="alert">
                    {problem(cancelAnalysis.error ?? retryAnalysis.error)}
                  </p>
                )}
                {structuralReport.data ? (
                  <StructuralReportView
                    report={structuralReport.data}
                    repositoryId={repository}
                  />
                ) : (
                  <div>
                    <p>
                      Analyze syntax trees, symbol references, and framework
                      boundaries for this snapshot.
                    </p>
                    {analyze.isPending ||
                    (analysisJob.data &&
                      !terminal.has(analysisJob.data.state)) ? (
                      <p role="status">
                        Analyzing architecture (
                        {analysisJob.data?.state ?? "starting"})…
                      </p>
                    ) : (
                      <button
                        type="button"
                        disabled={busy || analyze.isPending}
                        onClick={() => analyze.mutate()}
                      >
                        Analyze architecture
                      </button>
                    )}
                    {analyze.isError && (
                      <p role="alert">{problem(analyze.error)}</p>
                    )}
                    {analysisJob.data?.state === "failed" && (
                      <p role="alert">
                        Structural analysis failed:{" "}
                        {analysisJob.data.failure_code ?? "unknown error"}.
                      </p>
                    )}
                  </div>
                )}
              </section>
            </WorkspacePane>
          )}
          {snapshot && repository && (
            <WorkspacePane id="findings-title">
              <section aria-labelledby="findings-title">
                <div className="section-heading">
                  <h2 id="findings-title" tabIndex={-1}>
                    Deterministic Security Findings
                  </h2>
                  <span className="muted">
                    Multi-scanner static analysis & human triage
                  </span>
                </div>
                {scanConfiguration.isPending && (
                  <p role="status">Checking server scan policy…</p>
                )}
                {scanConfiguration.isError && (
                  <p role="alert">
                    Scan configuration could not be loaded.{" "}
                    <button
                      type="button"
                      onClick={() => void scanConfiguration.refetch()}
                    >
                      Retry scan configuration
                    </button>
                  </p>
                )}
                {scanConfiguration.data && (
                  <p className="muted">
                    {scanConfiguration.data.configured
                      ? `Selected scope: ${scanConfiguration.data.selected_source_files} source files and ${scanConfiguration.data.selected_lockfiles} lockfiles. Omitted by limits: ${scanConfiguration.data.omitted_source_files} source files and ${scanConfiguration.data.omitted_lockfiles} lockfiles. Configuration does not prove runtime health.`
                      : `Scanning is unavailable: ${scanConfiguration.data.reason?.replaceAll("_", " ") ?? "configuration unavailable"}. Configure the qualified scanner and server policy before starting.`}
                  </p>
                )}
                {scanJob.isError && (
                  <p role="alert">
                    Scan status is unavailable.{" "}
                    <button
                      type="button"
                      onClick={() => void scanJob.refetch()}
                    >
                      Reload scan status
                    </button>
                  </p>
                )}
                {scanJob.data && !terminal.has(scanJob.data.state) && (
                  <button
                    type="button"
                    disabled={cancelScan.isPending}
                    onClick={() => cancelScan.mutate()}
                  >
                    Cancel security scan
                  </button>
                )}
                {scanJob.data &&
                  ["failed", "cancelled"].includes(scanJob.data.state) && (
                    <button
                      type="button"
                      disabled={retryScan.isPending}
                      onClick={() => retryScan.mutate()}
                    >
                      Retry security scan
                    </button>
                  )}
                {(cancelScan.isError || retryScan.isError) && (
                  <p role="alert">
                    {problem(cancelScan.error ?? retryScan.error)}
                  </p>
                )}
                {scanJob.data?.result_run_id ? (
                  <FindingsWorkbench
                    repositoryId={repository}
                    snapshotId={snapshot}
                    runId={scanJob.data.result_run_id}
                    onInvestigate={(findingId, runId) => {
                      if (!snapshot) return;
                      setFindingTarget({
                        repositoryId: repository,
                        snapshotId: snapshot,
                        findingId,
                        runId,
                      });
                      const url = new URL(window.location.href);
                      url.searchParams.set("finding", findingId);
                      url.searchParams.set("finding_run", runId);
                      url.searchParams.delete("investigation");
                      url.hash = "workbench-title";
                      window.history.pushState(window.history.state, "", url);
                      window.dispatchEvent(new PopStateEvent("popstate"));
                    }}
                    csrfToken={session.data?.csrf_token ?? ""}
                  />
                ) : (
                  <div>
                    <p>
                      Run deterministic static analysis across SAST, Secrets,
                      SCA, and IaC rules for this snapshot.
                    </p>
                    {startScan.isPending ||
                    (scanJob.data && !terminal.has(scanJob.data.state)) ? (
                      <p role="status">
                        Running security scan (
                        {scanJob.data?.state ?? "starting"}
                        )…
                      </p>
                    ) : (
                      <button
                        type="button"
                        disabled={
                          busy ||
                          startScan.isPending ||
                          !scanConfiguration.data?.configured
                        }
                        onClick={() => startScan.mutate()}
                      >
                        Scan repository
                      </button>
                    )}
                    {startScan.isError && (
                      <p role="alert">{problem(startScan.error)}</p>
                    )}
                    {scanJob.data?.state === "failed" && (
                      <p role="alert">
                        Security scan failed:{" "}
                        {scanJob.data.failure_code ?? "unknown error"}.
                      </p>
                    )}
                  </div>
                )}
              </section>
            </WorkspacePane>
          )}
          {snapshot && repository && (
            <WorkspacePane id="retrieval-title">
              <RetrievalWorkbench
                repositoryId={repository}
                snapshotId={snapshot}
                csrfToken={session.data?.csrf_token ?? ""}
              />
            </WorkspacePane>
          )}
          {snapshot && repository && (
            <WorkspacePane id="workbench-title">
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
            </WorkspacePane>
          )}
        </>
      )}
    </WorkspaceShell>
  );
}

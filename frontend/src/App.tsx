import { useEffect, useRef, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  useInfiniteQuery,
} from "@tanstack/react-query";
import { api, ApiError, mutationHeaders, request } from "./api/client";
import type {
  SubmissionRecordV1,
  RetryResponseV1,
  RetryResponseV2,
} from "./api/generated/types.gen";
import { StructuralReportView } from "./features/structure/StructuralReportView";

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
const label = (value: string) => value.replaceAll("_", " ");
function problem(error: unknown): string {
  if (error instanceof ApiError && error.status === 401)
    return "Your session expired. Sign in again; your selected file is kept here.";
  if (error instanceof ApiError && error.status === 403)
    return "Access changed. Reload your session and repository list.";
  if (error instanceof ApiError && error.status === 409)
    return "This request conflicts with existing work. Reload its status before retrying.";
  if (error instanceof ApiError && error.status === 429)
    return "The work limit has been reached. Wait for current work to finish.";
  return "The request could not be confirmed. Reload its status before starting new work.";
}

export default function App() {
  const client = useQueryClient();
  const [repository, setRepository] = useState("");
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
  const input = useRef<HTMLInputElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const uploadKey = useRef(crypto.randomUUID());
  const retryKeys = useRef(new Map<string, string>());
  useEffect(() => {
    const url = new URL(window.location.href);
    for (const [name, value] of [
      ["submission", submissionId],
      ["job", jobId],
    ]) {
      if (value) url.searchParams.set(name!, value);
      else url.searchParams.delete(name!);
    }
    window.history.replaceState({}, "", url);
  }, [submissionId, jobId]);
  const session = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => api.session(signal),
    retry: false,
  });
  const repositories = useInfiniteQuery({
    queryKey: ["repositories"],
    initialPageParam: null as string | null,
    queryFn: ({ signal, pageParam }) => api.repositories(signal, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!session.data,
  });
  const repositoryItems =
    repositories.data?.pages.flatMap((page) => page.items) ?? [];
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
    window.history.replaceState({}, "", url);
  }, [snapshot]);
  const inventory = useQuery({
    queryKey: ["inventory", snapshot, pages.at(-1)],
    queryFn: ({ signal }) =>
      api.inventory(snapshot!, pages.at(-1) ?? null, signal),
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
      if (!repository || !snapshot || !session.data)
        throw new Error("missing input");
      const res = await api.createAnalysis(
        repository,
        snapshot,
        session.data.csrf_token,
      );
      setAnalysisJobId(res.job_id);
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
      setAnalysisJobId(result.retry_job_id);
    },
  });
  const upload = useMutation({
    mutationFn: async () => {
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
      setSubmissionId(reserved.id);
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
      setJobId(result.job_id ?? null);
      await client.invalidateQueries({ queryKey: ["submission", reserved.id] });
    },
  });
  const action = useMutation({
    mutationFn: async (kind: "cancel" | "retry") => {
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
      if (kind === "retry" && "retry_job_id" in result) {
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
  const busy = upload.isPending || action.isPending;
  return (
    <>
      <header>
        <a href="/" className="brand">
          CodeSPY <span>source desk</span>
        </a>
        <span>{session.data ? "Session active" : "Session unavailable"}</span>
      </header>
      <main>
        <div className="intro">
          <p className="eyebrow">Source intake / Phase 2</p>
          <h1>Source inventory</h1>
          <p>
            Inspect the files in a ZIP archive, their byte hashes, and what can
            be analyzed next.
          </p>
        </div>
        {session.isPending && <p role="status">Checking your session…</p>}
        {session.isError && (
          <section role="alert">
            <h2>Sign-in is unavailable</h2>
            <p>
              The local fixture or GitHub sign-in provider must be configured
              before you can submit sources.
            </p>
            <button onClick={() => void session.refetch()}>
              Retry session
            </button>
            <a href="/api/v1/auth/github/start">Sign in with GitHub</a>
          </section>
        )}
        {session.data && (
          <>
            <section aria-labelledby="upload-title">
              <h2 id="upload-title">Add a source archive</h2>
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
              {repositories.isSuccess && repositoryItems.length === 0 && (
                <p>No repositories are available.</p>
              )}
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
                    setRepository(event.target.value);
                    uploadKey.current = crypto.randomUUID();
                  }}
                >
                  <option value="">Choose a repository</option>
                  {repositoryItems
                    .filter((repo) => repo.source_kind !== "github")
                    .map((repo) => (
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
                  disabled={busy || !!submissionId}
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
                    (!!submissionId && !upload.isError)
                  }
                  aria-busy={upload.isPending}
                >
                  {upload.isPending ? "Uploading…" : "Upload archive"}
                </button>
              </form>
              {upload.isError && <p role="alert">{problem(upload.error)}</p>}
            </section>
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
                    window.history.replaceState(
                      {},
                      "",
                      window.location.pathname,
                    );
                    setRestoredSnapshot(null);
                    setSubmissionId(null);
                    setJobId(null);
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
            <section aria-labelledby="inventory-title">
              <div className="section-heading">
                <h2 id="inventory-title">File inventory</h2>
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
            {snapshot && repository && (
              <section aria-labelledby="structural-title">
                <div className="section-heading">
                  <h2 id="structural-title">Structural Architecture</h2>
                  <span className="muted">
                    Deterministic syntax & graph analysis · Phase 3
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
            )}
          </>
        )}
      </main>
      <footer>CodeSPY · Source admission and inventory</footer>
    </>
  );
}

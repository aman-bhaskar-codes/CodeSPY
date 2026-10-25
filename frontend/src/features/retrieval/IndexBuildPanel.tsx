import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";

interface Props {
  repositoryId: string;
  snapshotId: string;
  csrfToken: string;
}
const terminal = new Set(["succeeded", "partial", "failed", "cancelled"]);
function stored(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}
function save(key: string, value: string | null) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    /* Session storage may be unavailable; in-memory command identity still applies. */
  }
}

export function IndexBuildPanel(props: Props) {
  return (
    <IndexBuildState
      key={`${props.repositoryId}:${props.snapshotId}`}
      {...props}
    />
  );
}
function IndexBuildState({ repositoryId, snapshotId, csrfToken }: Props) {
  const client = useQueryClient();
  const prefix = `codespy:index:${repositoryId}:${snapshotId}`;
  const [jobId, setJobId] = useState<string | null>(() =>
    stored(`${prefix}:job`),
  );
  const [confirmCancel, setConfirmCancel] = useState(false);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const commands = useRef<Record<string, string>>({});
  const availabilityKey = ["index-availability", repositoryId, snapshotId];
  const statusKey = ["index-job", repositoryId, snapshotId, jobId];
  const availability = useQuery({
    queryKey: availabilityKey,
    queryFn: ({ signal }) =>
      api.indexAvailability(repositoryId, snapshotId, signal),
    retry: false,
  });
  const status = useQuery({
    queryKey: statusKey,
    queryFn: ({ signal }) =>
      api.indexJob(repositoryId, snapshotId, jobId!, signal),
    enabled: !!jobId,
    retry: false,
    refetchInterval: (query) =>
      terminal.has(query.state.data?.job.state ?? "")
        ? false
        : query.state.status === "error"
          ? false
          : 1500,
  });
  const state = status.data?.job.state;
  useEffect(() => {
    if (state && terminal.has(state))
      void client.invalidateQueries({
        queryKey: ["index-availability", repositoryId, snapshotId],
      });
  }, [state, client, repositoryId, snapshotId]);
  const commandKey = (action: string) => {
    const storageKey = `${prefix}:${action}`;
    const key =
      commands.current[action] ?? stored(storageKey) ?? crypto.randomUUID();
    commands.current[action] = key;
    save(storageKey, key);
    return key;
  };
  const selectJob = (id: string, action: string) => {
    setJobId(id);
    save(`${prefix}:job`, id);
    delete commands.current[action];
    save(`${prefix}:${action}`, null);
  };
  const create = useMutation({
    mutationFn: () =>
      api.createIndex(repositoryId, snapshotId, csrfToken, commandKey("build")),
    onSuccess: (value) => selectJob(value.job.id, "build"),
  });
  const retry = useMutation({
    mutationFn: () =>
      api.retryIndex(
        repositoryId,
        snapshotId,
        jobId!,
        csrfToken,
        commandKey(`retry:${jobId}`),
      ),
    onSuccess: (value) => selectJob(value.retry_job_id, `retry:${jobId}`),
  });
  const cancel = useMutation({
    mutationFn: () =>
      api.cancelIndex(repositoryId, snapshotId, jobId!, csrfToken),
    onSuccess: (value) => {
      client.setQueryData(statusKey, value);
      setConfirmCancel(false);
      cancelButton.current?.focus();
    },
  });
  const error = create.error ?? retry.error ?? cancel.error;
  const busy = create.isPending || retry.isPending || cancel.isPending;
  const active = !!jobId && (!state || !terminal.has(state));
  const coverage = availability.data?.coverage;
  return (
    <section
      aria-labelledby="source-index-title"
      style={{
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: 18,
        marginBottom: 18,
      }}
    >
      <div className="section-heading">
        <h3 id="source-index-title">Source index</h3>
        <button
          type="button"
          className="primary"
          disabled={!availability.data?.can_build || busy || active}
          onClick={() => create.mutate()}
        >
          {create.isError ? "Retry build request" : "Build source index"}
        </button>
      </div>
      <div role="status" aria-live="polite" style={{ minHeight: 48 }}>
        {create.isPending && <p>Submitting index build…</p>}
        {availability.isPending ? (
          <p className="muted">Checking source index…</p>
        ) : availability.data?.chunk_set_id ? (
          <>
            <p>
              Published set:{" "}
              <code style={{ overflowWrap: "anywhere" }}>
                {availability.data.chunk_set_id}
              </code>
            </p>
            {coverage && (
              <p>
                {coverage.processed_files} of {coverage.admitted_files} admitted
                files indexed.{" "}
                {coverage.processed_files < coverage.admitted_files
                  ? "Partial coverage; excluded files are not searched."
                  : "All admitted files processed."}
              </p>
            )}
            {coverage?.omissions?.map((item) => (
              <p key={`${item.reason}:${item.unit}`} className="muted">
                {item.reason.replaceAll("_", " ")}: {item.count} {item.unit}(s)
              </p>
            ))}
            <details>
              <summary>Index provenance</summary>
              <p>
                Configuration:{" "}
                <code style={{ overflowWrap: "anywhere" }}>
                  {availability.data.config_hash}
                </code>
              </p>
              <p>
                Materialization:{" "}
                <code style={{ overflowWrap: "anywhere" }}>
                  {availability.data.materialization_hash}
                </code>
              </p>
            </details>
          </>
        ) : (
          <p className="muted">
            No published source index. Build one before searching admitted
            files.
          </p>
        )}
        {jobId && (
          <p>
            Build status: {state ?? "loading"}. Job:{" "}
            <code style={{ overflowWrap: "anywhere" }}>{jobId}</code>
          </p>
        )}
        {active && availability.data?.chunk_set_id && (
          <p className="muted">
            The published set remains available while this build runs. Existing
            search results keep their original set.
          </p>
        )}
        {status.data?.job.failure_code && (
          <p>
            Build result: {status.data.job.failure_code.replaceAll("_", " ")}
          </p>
        )}
      </div>
      {availability.isError && (
        <p role="alert">
          Cannot check source index: {availability.error.message}.{" "}
          <button type="button" onClick={() => void availability.refetch()}>
            Reload index availability
          </button>
        </p>
      )}
      {status.isError && (
        <p role="alert">
          Cannot refresh build status: {status.error.message}.{" "}
          <button type="button" onClick={() => void status.refetch()}>
            Reload build status
          </button>
        </p>
      )}
      {error && (
        <p role="alert">
          Index request failed: {error.message}. Retry the same request to
          recover an uncertain response.
        </p>
      )}
      {!availability.data?.can_build &&
        !availability.isPending &&
        !availability.isError && (
          <p className="muted">
            Building is unavailable for your current access or runtime
            configuration.
          </p>
        )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        {status.data?.can_cancel && (
          <button
            ref={cancelButton}
            type="button"
            disabled={busy}
            onClick={() => setConfirmCancel(true)}
          >
            Cancel index build
          </button>
        )}
        {status.data?.can_retry && (
          <button type="button" disabled={busy} onClick={() => retry.mutate()}>
            Retry index build
          </button>
        )}
      </div>
      {confirmCancel && (
        <div role="group" aria-label="Confirm index build cancellation">
          <p>
            Cancel this index build? The published source index will remain
            available.
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setConfirmCancel(false);
              cancelButton.current?.focus();
            }}
          >
            Keep building
          </button>{" "}
          <button type="button" disabled={busy} onClick={() => cancel.mutate()}>
            Confirm cancellation
          </button>
        </div>
      )}
    </section>
  );
}

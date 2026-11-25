// Moved verbatim from the workspace composition; behaviour is unchanged.
import { label, problem, terminal } from "../problems";
import { useWorkspace } from "../workspaceContext";
export function JobStatusPane() {
  const {
    job,
    submission,
    selectedJob,
    busy,
    intake,
    action,
    cancelButtonRef,
  } = useWorkspace();
  const { selection, analysis } = useWorkspace();
  const {
    confirmCancel,
    setConfirmCancel,
    locationSearchRef,
    setRestoredSnapshot,
    setSubmissionId,
    setJobId,
    setScanJobId,
    setPages,
    setAnalysisJobId,
  } = selection;
  const { upload, setFile, inputRef, uploadKeyRef } = intake;
  const { analyze } = analysis;
  return (
    <>
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
            ref={cancelButtonRef}
            disabled={busy}
            onClick={() => setConfirmCancel(true)}
          >
            Cancel work
          </button>
        )}
        {confirmCancel && (
          <div className="confirmation">
            <p>
              Cancel this job? Verified input is retained for a limited time so
              you can request a retry.
            </p>
            <button
              autoFocus
              onClick={() => {
                setConfirmCancel(false);
                cancelButtonRef.current?.focus();
              }}
            >
              Keep working
            </button>
            <button disabled={busy} onClick={() => action.mutate("cancel")}>
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
              locationSearchRef.current = url.search;
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
              uploadKeyRef.current = crypto.randomUUID();
              if (inputRef.current) inputRef.current.value = "";
            }}
          >
            Start another submission
          </button>
        )}
      </section>
    </>
  );
}

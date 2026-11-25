// Moved verbatim from the workspace composition; behaviour is unchanged.
import { FindingsWorkbench } from "../../features/findings/FindingsWorkbench";
import { problem, terminal } from "../problems";
import { useWorkspace } from "../workspaceContext";
import type { SnapshotPaneProps } from "./types";
export function FindingsPane({ repository, snapshot }: SnapshotPaneProps) {
  const { session, busy, scan, selection } = useWorkspace();
  const { scanConfiguration, scanJob, cancelScan, retryScan, startScan } = scan;
  const { setFindingTarget } = selection;
  return (
    <>
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
            <button type="button" onClick={() => void scanJob.refetch()}>
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
          <p role="alert">{problem(cancelScan.error ?? retryScan.error)}</p>
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
              Run deterministic static analysis across SAST, Secrets, SCA, and
              IaC rules for this snapshot.
            </p>
            {startScan.isPending ||
            (scanJob.data && !terminal.has(scanJob.data.state)) ? (
              <p role="status">
                Running security scan ({scanJob.data?.state ?? "starting"}
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
    </>
  );
}

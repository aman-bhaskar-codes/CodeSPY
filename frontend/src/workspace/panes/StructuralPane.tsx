// Moved verbatim from the workspace composition; behaviour is unchanged.
import { ArchitectureWorkbench } from "../../features/architecture/ArchitectureWorkbench";
import { StructuralReportView } from "../../features/structure/StructuralReportView";
import { problem, terminal } from "../problems";
import { useWorkspace } from "../workspaceContext";
import type { SnapshotPaneProps } from "./types";
export function StructuralPane({ repository, snapshot }: SnapshotPaneProps) {
  const { session, busy, analysis } = useWorkspace();
  const {
    analysisJob,
    cancelAnalysis,
    retryAnalysis,
    structuralReport,
    analyze,
  } = analysis;
  return (
    <>
      <section aria-labelledby="structural-title">
        <div className="section-heading">
          <h2 id="structural-title" tabIndex={-1}>
            Structural Architecture
          </h2>
          <span className="muted">Deterministic syntax & graph analysis</span>
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
              Analyze syntax trees, symbol references, and framework boundaries
              for this snapshot.
            </p>
            {analyze.isPending ||
            (analysisJob.data && !terminal.has(analysisJob.data.state)) ? (
              <p role="status">
                Analyzing architecture ({analysisJob.data?.state ?? "starting"}
                )…
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
            {analyze.isError && <p role="alert">{problem(analyze.error)}</p>}
            {analysisJob.data?.state === "failed" && (
              <p role="alert">
                Structural analysis failed:{" "}
                {analysisJob.data.failure_code ?? "unknown error"}.
              </p>
            )}
          </div>
        )}
      </section>
      <ArchitectureWorkbench
        repositoryId={repository}
        snapshotId={snapshot}
        csrfToken={session.data?.csrf_token ?? ""}
      />
    </>
  );
}

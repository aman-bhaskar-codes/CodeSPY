import { type ExtendedSummary } from "../investigationTypes";
import { useInvestigations } from "../investigationsContext";

/** Title, status and the cancel, execute, resume and retry actions. */
export function DetailHeader() {
  const {
    executeMutation,
    cancelMutation,
    resumeMutation,
    retryMutation,
    summary: maybeSummary,
    actionAvailable,
    setCancelIntent,
  } = useInvestigations();
  if (!maybeSummary) return null;
  const summary = maybeSummary;
  return (
    <>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          borderBottom: "1px solid var(--cs-border)",
          paddingBottom: "16px",
          gap: "16px",
        }}
      >
        <div>
          <div
            style={{
              display: "flex",
              gap: "8px",
              alignItems: "center",
              marginBottom: "6px",
              flexWrap: "wrap",
            }}
          >
            <span
              id="investigation-status-badge"
              style={{
                fontSize: "11px",
                fontWeight: 700,
                padding: "2px 8px",
                borderRadius: "4px",
                background:
                  summary.status === "completed"
                    ? "var(--cs-success-bg)"
                    : summary.status === "awaiting_human_decision"
                      ? "var(--cs-warning-bg)"
                      : summary.status === "failed" ||
                          summary.status === "cancelled"
                        ? "var(--cs-danger-bg)"
                        : "var(--cs-info-bg)",
                color:
                  summary.status === "completed"
                    ? "var(--cs-success)"
                    : summary.status === "awaiting_human_decision"
                      ? "var(--cs-warning)"
                      : summary.status === "failed" ||
                          summary.status === "cancelled"
                        ? "var(--cs-danger)"
                        : "var(--cs-action)",
              }}
            >
              {summary.status.toUpperCase()}
            </span>
            <span style={{ fontSize: "12px", color: "var(--cs-muted)" }}>
              ID: {summary.id.slice(0, 8)}
            </span>
            {(summary as unknown as ExtendedSummary).current_job_id && (
              <span style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
                Job:{" "}
                {String(
                  (summary as unknown as ExtendedSummary).current_job_id,
                ).slice(0, 8)}
              </span>
            )}
          </div>
          <h4
            style={{
              margin: 0,
              fontSize: "16px",
              color: "var(--cs-text)",
            }}
          >
            {summary.query}
          </h4>
        </div>

        {/* Header Action Buttons */}
        <div
          style={{
            display: "flex",
            gap: "8px",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          {(summary.status === "pending" || summary.status === "queued") && (
            <button
              id="execute-investigation-btn"
              type="button"
              disabled={executeMutation.isPending}
              onClick={() => executeMutation.mutate(summary.id)}
              style={{
                padding: "8px 16px",
                borderRadius: "6px",
                border: "none",
                background: "var(--cs-success-bg)",
                color: "var(--cs-text)",
                fontWeight: 600,
                fontSize: "13px",
                cursor: executeMutation.isPending ? "not-allowed" : "pointer",
              }}
            >
              {executeMutation.isPending
                ? "Starting..."
                : "Execute Investigation"}
            </button>
          )}

          {actionAvailable(
            "cancel",
            [
              "running",
              "pending",
              "queued",
              "checkpointed",
              "awaiting_human_decision",
            ].includes(summary.status),
          ) && (
            <button
              id="cancel-investigation-btn"
              type="button"
              disabled={cancelMutation.isPending}
              onClick={() => setCancelIntent(summary.id)}
              style={{
                padding: "8px 16px",
                borderRadius: "6px",
                border: "none",
                background: "var(--cs-danger-bg)",
                color: "var(--cs-text)",
                fontWeight: 600,
                fontSize: "13px",
                cursor: cancelMutation.isPending ? "not-allowed" : "pointer",
              }}
            >
              {cancelMutation.isPending ? "Cancelling..." : "Cancel"}
            </button>
          )}

          {actionAvailable(
            "resume",
            summary.status === "awaiting_human_decision",
          ) && (
            <button
              id="resume-investigation-btn"
              type="button"
              disabled={resumeMutation.isPending}
              onClick={() => resumeMutation.mutate()}
              style={{
                padding: "8px 16px",
                borderRadius: "6px",
                border: "none",
                background: "var(--cs-warning-bg)",
                color: "var(--cs-text)",
                fontWeight: 600,
                fontSize: "13px",
                cursor: resumeMutation.isPending ? "not-allowed" : "pointer",
              }}
            >
              {resumeMutation.isPending ? "Resuming..." : "Resume"}
            </button>
          )}

          {actionAvailable(
            "retry",
            ["failed", "cancelled"].includes(summary.status),
          ) && (
            <button
              id="retry-investigation-btn"
              type="button"
              disabled={retryMutation.isPending}
              onClick={() => retryMutation.mutate()}
              style={{
                padding: "8px 16px",
                borderRadius: "6px",
                border: "none",
                background: "var(--cs-action)",
                color: "var(--cs-action-ink)",
                fontWeight: 600,
                fontSize: "13px",
                cursor: retryMutation.isPending ? "not-allowed" : "pointer",
              }}
            >
              {retryMutation.isPending ? "Retrying..." : "Retry Investigation"}
            </button>
          )}
        </div>
      </div>
    </>
  );
}

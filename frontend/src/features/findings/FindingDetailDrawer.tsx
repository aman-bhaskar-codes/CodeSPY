import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { FindingSupport } from "./FindingSupport";
import { FeatureTraceGraph } from "./FeatureTraceGraph";
import { TriageModal } from "./TriageModal";
import type {
  FindingDetailResponseV1,
  FindingSummaryV1,
  TriageTransitionV1,
} from "./types";

interface Props {
  repositoryId: string;
  snapshotId: string;
  findingId: string;
  csrfToken: string;
  isOpen: boolean;
  onClose: () => void;
  onTriageUpdated: () => void;
}

export function FindingDetailDrawer({
  repositoryId,
  snapshotId,
  findingId,
  csrfToken,
  isOpen,
  onClose,
  onTriageUpdated,
}: Props) {
  const [isTriageOpen, setIsTriageOpen] = useState(false);
  const [showTrace, setShowTrace] = useState(false);
  const historyScope = `${repositoryId}:${snapshotId}:${findingId}`;
  const [historyPage, setHistoryPage] = useState<{
    scope: string;
    cursor: string | null;
  }>({ scope: historyScope, cursor: null });
  const historyCursor =
    historyPage.scope === historyScope ? historyPage.cursor : null;

  const detailQuery = useQuery({
    queryKey: ["finding", repositoryId, snapshotId, findingId],
    queryFn: ({ signal }) =>
      api.findingDetail(repositoryId, snapshotId, findingId, signal),
    enabled: isOpen && !!findingId,
  });

  const historyQuery = useQuery({
    queryKey: [
      "finding-history",
      repositoryId,
      snapshotId,
      findingId,
      historyCursor,
    ],
    queryFn: ({ signal }) =>
      api.triageHistory(
        repositoryId,
        snapshotId,
        findingId,
        signal,
        historyCursor ?? undefined,
      ),
    enabled: isOpen && !!findingId,
  });

  const traceQuery = useQuery({
    queryKey: ["finding-trace", repositoryId, snapshotId, findingId],
    queryFn: () =>
      api.featureTrace(
        repositoryId,
        snapshotId,
        findingId,
        undefined,
        csrfToken,
      ),
    enabled: isOpen && showTrace && !!findingId,
  });

  if (!isOpen) return null;

  const finding = detailQuery.data;
  const detail: FindingDetailResponseV1 | undefined = detailQuery.data;

  const triageFinding: FindingSummaryV1 | undefined = finding
    ? {
        id: finding.id,
        run_id: finding.run_id,
        rule_id: finding.rule_id,
        category: finding.category,
        severity: finding.severity,
        title: finding.title,
        file_path: finding.file_path,
        line_number: finding.line_number,
        disposition: finding.disposition,
        triage_state: finding.triage.current_state,
        triage_reason: finding.triage.current_reason,
        triage_version: finding.triage.version,
      }
    : undefined;
  const recommendation =
    typeof detail?.recommendation?.action === "string"
      ? detail.recommendation.action
      : null;

  const handleTriageSubmit = async (
    transition: TriageTransitionV1,
    requestKey: string,
  ) => {
    await api.triageFinding(
      repositoryId,
      snapshotId,
      findingId,
      transition,
      csrfToken,
      requestKey,
    );
    await detailQuery.refetch();
    setHistoryPage({ scope: historyScope, cursor: null });
    await historyQuery.refetch();
    onTriageUpdated();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="finding-detail-title"
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(15, 23, 42, 0.6)",
        display: "flex",
        justifyContent: "flex-end",
        zIndex: 900,
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          background: "var(--surface)",
          width: "100%",
          maxWidth: "760px",
          height: "100%",
          overflowY: "auto",
          boxShadow: "-8px 0 25px rgba(0, 0, 0, 0.15)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "20px 24px",
            borderBottom: "1px solid var(--border)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: "12px",
            position: "sticky",
            top: 0,
            background: "var(--surface)",
            zIndex: 10,
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
                style={{
                  fontSize: "11px",
                  fontWeight: 700,
                  textTransform: "uppercase",
                  padding: "2px 8px",
                  borderRadius: "4px",
                  background:
                    finding?.severity === "critical"
                      ? "#fde8e8"
                      : finding?.severity === "high"
                        ? "#feeedb"
                        : "#f1f5f9",
                  color:
                    finding?.severity === "critical"
                      ? "#9c2929"
                      : finding?.severity === "high"
                        ? "#a55307"
                        : "var(--ink)",
                }}
              >
                {finding?.severity ?? "..."}
              </span>
              <span
                style={{
                  fontSize: "11px",
                  fontWeight: 700,
                  textTransform: "uppercase",
                  padding: "2px 8px",
                  borderRadius: "4px",
                  background: "#e8effc",
                  color: "var(--action)",
                }}
              >
                {finding?.category ?? "..."}
              </span>
              <span
                style={{
                  fontSize: "11px",
                  fontWeight: 600,
                  padding: "2px 8px",
                  borderRadius: "4px",
                  background: "#f1f5f9",
                  color: "var(--muted)",
                }}
              >
                disposition: {finding?.disposition ?? "..."}
              </span>
            </div>
            <h2
              id="finding-detail-title"
              style={{ margin: 0, fontSize: "20px", color: "var(--ink)" }}
            >
              {finding?.rule_id ?? "Finding Detail"}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close details"
            style={{
              background: "transparent",
              border: "1px solid var(--border)",
              borderRadius: "4px",
              padding: "6px 12px",
              cursor: "pointer",
              fontSize: "14px",
            }}
          >
            ✕ Close
          </button>
        </div>

        {/* Content */}
        <div style={{ padding: "24px", flex: 1, display: "grid", gap: "24px" }}>
          {detailQuery.isLoading && (
            <div
              style={{
                textAlign: "center",
                padding: "40px",
                color: "var(--muted)",
              }}
            >
              Loading finding details and verified code excerpts...
            </div>
          )}

          {detailQuery.isError && (
            <div
              role="alert"
              style={{
                padding: "16px",
                background: "#fde8e8",
                border: "1px solid var(--danger)",
                borderRadius: "6px",
                color: "var(--danger)",
              }}
            >
              Failed to load finding details. Reload or verify resource
              permissions.
            </div>
          )}

          {finding && (
            <>
              {/* Location & Message */}
              <div>
                <h4 style={{ margin: "0 0 8px", fontSize: "14px" }}>
                  Location & Message
                </h4>
                <div
                  style={{
                    padding: "12px",
                    background: "#f8fafc",
                    border: "1px solid var(--border)",
                    borderRadius: "6px",
                    fontSize: "13px",
                  }}
                >
                  <div style={{ marginBottom: "6px" }}>
                    <span className="muted">File:</span>{" "}
                    <code>
                      {finding.file_path}
                      {finding.line_number ? `:${finding.line_number}` : ""}
                    </code>
                  </div>
                  <div>
                    <span className="muted">Message:</span> {finding.message}
                  </div>
                </div>
              </div>

              <FindingSupport
                category={finding.category}
                disposition={finding.disposition}
                historicalDisposition={finding.historical_disposition}
                supportReason={finding.support_reason}
                excerpt={finding.code_excerpt}
                excerptReason={finding.excerpt_reason}
              />

              {/* Remediation */}
              {recommendation && (
                <div>
                  <h4 style={{ margin: "0 0 8px", fontSize: "14px" }}>
                    Remediation Guidance
                  </h4>
                  <div
                    style={{
                      padding: "12px",
                      background: "#f0fdf4",
                      border: "1px solid #bbf7d0",
                      borderRadius: "6px",
                      fontSize: "13px",
                      color: "#166534",
                    }}
                  >
                    {recommendation}
                  </div>
                </div>
              )}

              {detail && (
                <div>
                  <h4>Evidence support</h4>
                  <p>Current support: {detail.support_reason}</p>
                  <p>Historical disposition: {detail.historical_disposition}</p>
                  <ul>
                    {detail.evidence_ids?.map((id) => (
                      <li key={id}>
                        <code>{id}</code>
                      </li>
                    ))}
                  </ul>
                  {detail.limitations?.map((text, index) => (
                    <p key={index}>{text}</p>
                  ))}
                </div>
              )}

              {/* Triage Status & Controls */}
              <div
                style={{
                  padding: "16px",
                  background: "#f8fafc",
                  border: "1px solid var(--border)",
                  borderRadius: "8px",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    flexWrap: "wrap",
                    gap: "12px",
                  }}
                >
                  <div>
                    <h4 style={{ margin: "0 0 4px", fontSize: "14px" }}>
                      Human Triage State
                    </h4>
                    <div style={{ fontSize: "13px" }}>
                      Current: <strong>{finding.triage.current_state}</strong>
                      {finding.triage.current_reason !== "none" && (
                        <span>
                          {" "}
                          · reason: <em>{finding.triage.current_reason}</em>
                        </span>
                      )}
                      <span className="muted">
                        {" "}
                        (v{finding.triage.version})
                      </span>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setIsTriageOpen(true)}
                    style={{
                      background: "var(--action)",
                      color: "#fff",
                      border: "none",
                      padding: "8px 16px",
                      borderRadius: "6px",
                      fontWeight: 600,
                      cursor: "pointer",
                    }}
                  >
                    Change Triage State
                  </button>
                </div>

                {/* Triage History Trail */}
                {historyQuery.data?.events &&
                  historyQuery.data.events.length > 0 && (
                    <div style={{ marginTop: "16px" }}>
                      <h5
                        style={{
                          margin: "0 0 8px",
                          fontSize: "12px",
                          textTransform: "uppercase",
                          color: "var(--muted)",
                        }}
                      >
                        Audit Trail ({historyQuery.data.events.length} decisions
                        on this page)
                      </h5>
                      <div
                        style={{
                          display: "grid",
                          gap: "6px",
                          fontSize: "12px",
                        }}
                      >
                        {historyQuery.data.events.map((ev) => (
                          <div
                            key={ev.event_id}
                            style={{
                              padding: "6px 10px",
                              background: "var(--surface)",
                              border: "1px solid var(--border)",
                              borderRadius: "4px",
                              display: "flex",
                              justifyContent: "space-between",
                            }}
                          >
                            <div>
                              <strong>{ev.state}</strong>
                              {ev.reason !== "none" && (
                                <span> ({ev.reason})</span>
                              )}
                            </div>
                            <span
                              className="muted"
                              style={{ fontSize: "11px" }}
                            >
                              {new Date(ev.created_at).toLocaleString()}
                            </span>
                          </div>
                        ))}
                      </div>
                      {historyQuery.data.next_cursor && (
                        <button
                          type="button"
                          disabled={historyQuery.isFetching}
                          onClick={() =>
                            setHistoryPage({
                              scope: historyScope,
                              cursor: historyQuery.data!.next_cursor!,
                            })
                          }
                        >
                          Older decisions
                        </button>
                      )}
                      {historyCursor && (
                        <button
                          type="button"
                          onClick={() =>
                            setHistoryPage({
                              scope: historyScope,
                              cursor: null,
                            })
                          }
                        >
                          Newest decisions
                        </button>
                      )}
                    </div>
                  )}
              </div>

              {/* Feature Trace Section */}
              <div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    marginBottom: "10px",
                  }}
                >
                  <h4 style={{ margin: 0, fontSize: "14px" }}>
                    Native Feature Trace
                  </h4>
                  <button
                    type="button"
                    onClick={() => setShowTrace(!showTrace)}
                    style={{
                      background: "transparent",
                      border: "1px solid var(--border)",
                      borderRadius: "4px",
                      padding: "6px 12px",
                      fontSize: "12px",
                      cursor: "pointer",
                    }}
                  >
                    {showTrace ? "Hide Trace" : "Trace 2-Hop CPG Graph"}
                  </button>
                </div>

                {showTrace && (
                  <div>
                    {traceQuery.isLoading && (
                      <div
                        style={{
                          padding: "20px",
                          textAlign: "center",
                          color: "var(--muted)",
                          fontSize: "13px",
                        }}
                      >
                        Loading supported structural relationships...
                      </div>
                    )}
                    {traceQuery.isError && (
                      <div
                        role="alert"
                        style={{
                          padding: "12px",
                          background: "#fde8e8",
                          border: "1px solid var(--danger)",
                          borderRadius: "6px",
                          color: "var(--danger)",
                          fontSize: "13px",
                        }}
                      >
                        Failed to compute feature trace.
                      </div>
                    )}
                    {traceQuery.data && (
                      <FeatureTraceGraph trace={traceQuery.data} />
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Triage Action Modal */}
      {triageFinding && (
        <TriageModal
          finding={triageFinding}
          isOpen={isTriageOpen}
          onClose={() => setIsTriageOpen(false)}
          onSubmit={handleTriageSubmit}
        />
      )}
    </div>
  );
}

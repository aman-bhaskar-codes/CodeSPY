import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type {
  InvestigationClaimV1,
  InvestigationDetailResponseV1,
  InvestigationEventV1,
  InvestigationFeedbackRequestV1,
  InvestigationMode,
  InvestigationVerdict,
} from "./types";

interface Props {
  repositoryId: string;
  snapshotId?: string;
  findingId?: string;
  csrfToken: string;
  onFindingTriageRequested?: (findingId: string) => void;
}

export function InvestigationsWorkbench({
  repositoryId,
  snapshotId,
  findingId: initialFindingId,
  csrfToken,
}: Props) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [findingId, setFindingId] = useState(initialFindingId || "");
  const [mode, setMode] = useState<InvestigationMode>("short_path");
  const [maxSteps, setMaxSteps] = useState(10);
  const [budgetMicros, setBudgetMicros] = useState(5_000_000); // $5.00
  const [selectedInvId, setSelectedInvId] = useState<string | null>(null);
  const [feedbackNotes, setFeedbackNotes] = useState("");
  const [feedbackType, setFeedbackType] = useState<
    "agree" | "disagree" | "needs_review"
  >("agree");
  const [saveToMemory, setSaveToMemory] = useState(true);
  const [liveEvents, setLiveEvents] = useState<InvestigationEventV1[]>([]);

  // List past investigations
  const listQuery = useQuery({
    queryKey: ["investigations", repositoryId],
    queryFn: ({ signal }) =>
      api.listInvestigations(repositoryId, undefined, signal),
    enabled: !!repositoryId,
    refetchInterval: 5000,
  });

  // Selected investigation details
  const detailQuery = useQuery({
    queryKey: ["investigation-detail", repositoryId, selectedInvId],
    queryFn: ({ signal }) =>
      selectedInvId
        ? api.getInvestigation(repositoryId, selectedInvId, signal)
        : null,
    enabled: !!selectedInvId && !!repositoryId,
    refetchInterval: (query) => {
      const data = query.state.data as InvestigationDetailResponseV1 | null;
      if (!data) return false;
      const status = data.summary.status;
      return status === "running" || status === "pending" ? 2000 : false;
    },
  });

  // Create mutation
  const createMutation = useMutation({
    mutationFn: async () => {
      return await api.createInvestigation(
        repositoryId,
        {
          repository_id: repositoryId,
          query: query.trim(),
          finding_id: findingId.trim() || undefined,
          mode,
          max_steps: maxSteps,
          budget_limit_micros: budgetMicros,
        },
        csrfToken,
      );
    },
    onSuccess: (newInv) => {
      setSelectedInvId(newInv.id);
      setLiveEvents([]);
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
    },
  });

  // Execute mutation
  const executeMutation = useMutation({
    mutationFn: async (id: string) => {
      return await api.executeInvestigation(repositoryId, id, csrfToken);
    },
    onSuccess: (result) => {
      queryClient.setQueryData(
        ["investigation-detail", repositoryId, selectedInvId],
        result,
      );
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
    },
  });

  // Feedback mutation
  const feedbackMutation = useMutation({
    mutationFn: async (fb: InvestigationFeedbackRequestV1) => {
      if (!selectedInvId) return;
      return await api.submitInvestigationFeedback(
        repositoryId,
        selectedInvId,
        fb,
        csrfToken,
      );
    },
    onSuccess: () => {
      setFeedbackNotes("");
      queryClient.invalidateQueries({
        queryKey: ["investigation-detail", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
    },
  });

  // Server-Sent Events (SSE) Live Stream Subscription
  useEffect(() => {
    if (!selectedInvId) return;
    const sseUrl = `/api/v1/repositories/${repositoryId}/investigations/${selectedInvId}/stream`;
    const eventSource = new EventSource(sseUrl);

    eventSource.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as InvestigationEventV1;
        setLiveEvents((prev) => {
          if (prev.some((item) => item.id === parsed.id)) return prev;
          return [...prev, parsed];
        });
      } catch {
        // Ignored unparsable SSE ping
      }
    };

    eventSource.addEventListener("step_started", (e: MessageEvent) => {
      try {
        const parsed = JSON.parse(e.data) as InvestigationEventV1;
        setLiveEvents((prev) => [...prev, parsed]);
      } catch {
        // Ignored unparsable SSE ping
      }
    });

    eventSource.addEventListener("verdict_reached", () => {
      queryClient.invalidateQueries({
        queryKey: ["investigation-detail", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
      eventSource.close();
    });

    eventSource.onerror = () => {
      eventSource.close();
    };

    return () => {
      eventSource.close();
    };
  }, [selectedInvId, repositoryId, queryClient]);

  const detail = detailQuery.data;
  const summary = detail?.summary;

  const formatMicros = (micros: number) => {
    return `$${(micros / 1_000_000).toFixed(4)}`;
  };

  const getVerdictStyle = (verdict?: InvestigationVerdict | null) => {
    switch (verdict) {
      case "true_positive":
        return {
          background: "#fee2e2",
          color: "#991b1b",
          border: "1px solid #f87171",
        };
      case "false_positive":
        return {
          background: "#dcfce7",
          color: "#166534",
          border: "1px solid #4ade80",
        };
      case "benign":
        return {
          background: "#e0f2fe",
          color: "#0369a1",
          border: "1px solid #38bdf8",
        };
      default:
        return {
          background: "#fef3c7",
          color: "#92400e",
          border: "1px solid #fcd34d",
        };
    }
  };

  const getClaimBadgeStyle = (status: InvestigationClaimV1["status"]) => {
    switch (status) {
      case "verified":
        return {
          background: "#dcfce7",
          color: "#166534",
          label: "VERIFIED GROUNDING",
        };
      case "refuted":
        return {
          background: "#fee2e2",
          color: "#991b1b",
          label: "REFUTED CITATION",
        };
      case "unsupported":
        return {
          background: "#fef3c7",
          color: "#92400e",
          label: "UNSUPPORTED",
        };
      default:
        return { background: "#f1f5f9", color: "#475569", label: "PENDING" };
    }
  };

  return (
    <div
      id="investigations-workbench"
      style={{
        padding: "24px",
        display: "flex",
        flexDirection: "column",
        gap: "24px",
        background: "#0f172a",
        color: "#f8fafc",
        borderRadius: "12px",
        fontFamily: "Inter, system-ui, sans-serif",
      }}
    >
      {/* Header Banner */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          borderBottom: "1px solid #334155",
          paddingBottom: "16px",
        }}
      >
        <div>
          <h2
            id="workbench-title"
            style={{
              margin: 0,
              fontSize: "20px",
              fontWeight: 700,
              color: "#38bdf8",
            }}
          >
            Agentic Security Investigations
          </h2>
          <p style={{ margin: "4px 0 0", fontSize: "13px", color: "#94a3b8" }}>
            Evidence-grounded model orchestration, anti-hallucination claim
            verification, and durable fenced execution.
          </p>
        </div>
        <div style={{ display: "flex", gap: "12px", alignItems: "center" }}>
          <div
            style={{
              padding: "6px 12px",
              background: "#1e293b",
              borderRadius: "6px",
              fontSize: "12px",
              border: "1px solid #334155",
            }}
          >
            <span style={{ color: "#94a3b8" }}>Mode: </span>
            <strong style={{ color: "#38bdf8" }}>
              {mode === "short_path" ? "Short Path" : "Two Specialist"}
            </strong>
          </div>
          {snapshotId && (
            <div
              style={{
                padding: "6px 12px",
                background: "#1e293b",
                borderRadius: "6px",
                fontSize: "12px",
                border: "1px solid #334155",
              }}
            >
              <span style={{ color: "#94a3b8" }}>Snapshot: </span>
              <strong style={{ color: "#f8fafc" }}>
                {snapshotId.slice(0, 8)}
              </strong>
            </div>
          )}
          <div
            style={{
              padding: "6px 12px",
              background: "#1e293b",
              borderRadius: "6px",
              fontSize: "12px",
              border: "1px solid #334155",
            }}
          >
            <span style={{ color: "#94a3b8" }}>Budget Limit: </span>
            <strong style={{ color: "#a855f7" }}>
              {formatMicros(budgetMicros)}
            </strong>
          </div>
        </div>
      </div>

      {/* Main Grid: Left Form/History, Right Active Workbench */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1.6fr",
          gap: "24px",
        }}
      >
        {/* Left Column: Launch Form & History */}
        <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          {/* New Investigation Form */}
          <div
            style={{
              padding: "16px",
              background: "#1e293b",
              borderRadius: "8px",
              border: "1px solid #334155",
              display: "flex",
              flexDirection: "column",
              gap: "12px",
            }}
          >
            <h3
              style={{
                margin: 0,
                fontSize: "14px",
                fontWeight: 600,
                color: "#f1f5f9",
              }}
            >
              Launch New Investigation
            </h3>

            <div>
              <label
                htmlFor="investigation-query-input"
                style={{
                  display: "block",
                  fontSize: "12px",
                  color: "#94a3b8",
                  marginBottom: "4px",
                }}
              >
                Security Hypothesis or Finding to Investigate:
              </label>
              <textarea
                id="investigation-query-input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="e.g. Verify potential hardcoded API key in auth handler or validate SSRF path"
                rows={3}
                style={{
                  width: "100%",
                  padding: "8px",
                  borderRadius: "6px",
                  border: "1px solid #475569",
                  background: "#0f172a",
                  color: "#f8fafc",
                  fontSize: "13px",
                  boxSizing: "border-box",
                }}
              />
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "12px",
              }}
            >
              <div>
                <label
                  htmlFor="investigation-mode-select"
                  style={{
                    display: "block",
                    fontSize: "12px",
                    color: "#94a3b8",
                    marginBottom: "4px",
                  }}
                >
                  Orchestration Mode:
                </label>
                <select
                  id="investigation-mode-select"
                  value={mode}
                  onChange={(e) => setMode(e.target.value as InvestigationMode)}
                  style={{
                    width: "100%",
                    padding: "6px 8px",
                    borderRadius: "6px",
                    border: "1px solid #475569",
                    background: "#0f172a",
                    color: "#f8fafc",
                    fontSize: "12px",
                  }}
                >
                  <option value="short_path">
                    Default Short-Path (Direct)
                  </option>
                  <option value="two_specialist">
                    Two-Specialist (Adversarial)
                  </option>
                </select>
              </div>

              <div>
                <label
                  htmlFor="investigation-finding-id-input"
                  style={{
                    display: "block",
                    fontSize: "12px",
                    color: "#94a3b8",
                    marginBottom: "4px",
                  }}
                >
                  Target Finding ID (Optional):
                </label>
                <input
                  id="investigation-finding-id-input"
                  type="text"
                  value={findingId}
                  onChange={(e) => setFindingId(e.target.value)}
                  placeholder="UUID (optional)"
                  style={{
                    width: "100%",
                    padding: "6px 8px",
                    borderRadius: "6px",
                    border: "1px solid #475569",
                    background: "#0f172a",
                    color: "#f8fafc",
                    fontSize: "12px",
                    boxSizing: "border-box",
                  }}
                />
              </div>
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "12px",
              }}
            >
              <div>
                <label
                  htmlFor="investigation-budget-select"
                  style={{
                    display: "block",
                    fontSize: "12px",
                    color: "#94a3b8",
                    marginBottom: "4px",
                  }}
                >
                  Max Cost Budget:
                </label>
                <select
                  id="investigation-budget-select"
                  aria-label="Max Cost Budget"
                  value={budgetMicros}
                  onChange={(e) => setBudgetMicros(Number(e.target.value))}
                  style={{
                    width: "100%",
                    padding: "6px 8px",
                    borderRadius: "6px",
                    border: "1px solid #475569",
                    background: "#0f172a",
                    color: "#f8fafc",
                    fontSize: "12px",
                  }}
                >
                  <option value={250_000}>$0.25 (Test)</option>
                  <option value={1_000_000}>$1.00 (Standard)</option>
                  <option value={5_000_000}>$5.00 (Deep)</option>
                  <option value={10_000_000}>$10.00 (Comprehensive)</option>
                </select>
              </div>

              <div>
                <label
                  htmlFor="investigation-max-steps-input"
                  style={{
                    display: "block",
                    fontSize: "12px",
                    color: "#94a3b8",
                    marginBottom: "4px",
                  }}
                >
                  Max Tool Steps:
                </label>
                <input
                  id="investigation-max-steps-input"
                  aria-label="Max Tool Steps"
                  type="number"
                  min={1}
                  max={30}
                  value={maxSteps}
                  onChange={(e) => setMaxSteps(Number(e.target.value))}
                  style={{
                    width: "100%",
                    padding: "6px 8px",
                    borderRadius: "6px",
                    border: "1px solid #475569",
                    background: "#0f172a",
                    color: "#f8fafc",
                    fontSize: "12px",
                    boxSizing: "border-box",
                  }}
                />
              </div>
            </div>

            <button
              id="start-investigation-btn"
              type="button"
              disabled={createMutation.isPending || !query.trim()}
              onClick={() => createMutation.mutate()}
              style={{
                marginTop: "8px",
                padding: "8px 16px",
                borderRadius: "6px",
                border: "none",
                background: "#0284c7",
                color: "#ffffff",
                fontWeight: 600,
                fontSize: "13px",
                cursor:
                  !query.trim() || createMutation.isPending
                    ? "not-allowed"
                    : "pointer",
                opacity: !query.trim() || createMutation.isPending ? 0.6 : 1,
              }}
            >
              {createMutation.isPending
                ? "Initializing..."
                : "Run Investigation"}
            </button>
          </div>

          {/* Past Investigations List */}
          <div
            style={{
              padding: "16px",
              background: "#1e293b",
              borderRadius: "8px",
              border: "1px solid #334155",
            }}
          >
            <h3
              style={{
                margin: "0 0 12px",
                fontSize: "14px",
                fontWeight: 600,
                color: "#f1f5f9",
              }}
            >
              Investigations ({listQuery.data?.total_count || 0})
            </h3>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "8px",
                maxHeight: "350px",
                overflowY: "auto",
              }}
            >
              {listQuery.data?.items.map((inv) => (
                <div
                  key={inv.id}
                  onClick={() => setSelectedInvId(inv.id)}
                  style={{
                    padding: "10px",
                    borderRadius: "6px",
                    background:
                      selectedInvId === inv.id ? "#334155" : "#0f172a",
                    border: `1px solid ${selectedInvId === inv.id ? "#38bdf8" : "#1e293b"}`,
                    cursor: "pointer",
                    display: "flex",
                    flexDirection: "column",
                    gap: "4px",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <span
                      style={{
                        fontSize: "12px",
                        fontWeight: 600,
                        color: "#38bdf8",
                      }}
                    >
                      {inv.query.slice(0, 36)}...
                    </span>
                    <span
                      style={{
                        fontSize: "11px",
                        padding: "2px 6px",
                        borderRadius: "4px",
                        ...getVerdictStyle(inv.verdict),
                      }}
                    >
                      {inv.verdict
                        ? inv.verdict.toUpperCase()
                        : inv.status.toUpperCase()}
                    </span>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: "11px",
                      color: "#94a3b8",
                    }}
                  >
                    <span>
                      Step: {inv.current_step}/{inv.max_steps}
                    </span>
                    <span>Spent: {formatMicros(inv.spent_micros)}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Right Column: Active Investigation Details & Execution */}
        <div
          style={{
            padding: "20px",
            background: "#1e293b",
            borderRadius: "8px",
            border: "1px solid #334155",
            display: "flex",
            flexDirection: "column",
            gap: "20px",
          }}
        >
          {summary ? (
            <>
              {/* Detail Header & Action */}
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "flex-start",
                  borderBottom: "1px solid #334155",
                  paddingBottom: "16px",
                }}
              >
                <div>
                  <div
                    style={{
                      display: "flex",
                      gap: "8px",
                      alignItems: "center",
                      marginBottom: "6px",
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
                            ? "#065f46"
                            : "#1e3a8a",
                        color:
                          summary.status === "completed"
                            ? "#34d399"
                            : "#60a5fa",
                      }}
                    >
                      {summary.status.toUpperCase()}
                    </span>
                    <span style={{ fontSize: "12px", color: "#64748b" }}>
                      ID: {summary.id.slice(0, 8)}
                    </span>
                  </div>
                  <h4 style={{ margin: 0, fontSize: "16px", color: "#f8fafc" }}>
                    {summary.query}
                  </h4>
                </div>

                {summary.status !== "completed" && (
                  <button
                    id="execute-investigation-btn"
                    type="button"
                    disabled={executeMutation.isPending}
                    onClick={() => executeMutation.mutate(summary.id)}
                    style={{
                      padding: "8px 16px",
                      borderRadius: "6px",
                      border: "none",
                      background: "#10b981",
                      color: "#ffffff",
                      fontWeight: 600,
                      fontSize: "13px",
                      cursor: executeMutation.isPending
                        ? "not-allowed"
                        : "pointer",
                    }}
                  >
                    {executeMutation.isPending
                      ? "Investigating..."
                      : "Execute Short-Path"}
                  </button>
                )}
              </div>

              {/* Verdict & Summary Banner */}
              {summary.verdict && (
                <div
                  id="verdict-banner"
                  style={{
                    padding: "16px",
                    borderRadius: "8px",
                    ...getVerdictStyle(summary.verdict),
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "8px",
                    }}
                  >
                    <strong style={{ fontSize: "15px" }}>
                      VERDICT: {summary.verdict.toUpperCase().replace("_", " ")}
                    </strong>
                    {summary.verdict_confidence != null && (
                      <span
                        id="verdict-confidence-badge"
                        style={{ fontSize: "13px", fontWeight: 600 }}
                      >
                        Calibrated Confidence:{" "}
                        {(summary.verdict_confidence * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>
                  <p style={{ margin: 0, fontSize: "13px", lineHeight: "1.5" }}>
                    {summary.summary}
                  </p>
                </div>
              )}

              {/* Verified Claims Section */}
              <div>
                <h4
                  style={{
                    margin: "0 0 12px",
                    fontSize: "14px",
                    fontWeight: 600,
                    color: "#38bdf8",
                  }}
                >
                  Evidence Claims ({detail.claims.length})
                </h4>
                {detail.claims.length === 0 ? (
                  <p
                    style={{
                      fontSize: "13px",
                      color: "#94a3b8",
                      fontStyle: "italic",
                    }}
                  >
                    No specific factual claims extracted yet.
                  </p>
                ) : (
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: "10px",
                    }}
                  >
                    {detail.claims.map((claim) => {
                      const badge = getClaimBadgeStyle(claim.status);
                      return (
                        <div
                          key={claim.claim_id}
                          style={{
                            padding: "12px",
                            background: "#0f172a",
                            borderRadius: "6px",
                            border: "1px solid #334155",
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              marginBottom: "6px",
                            }}
                          >
                            <strong
                              style={{ fontSize: "13px", color: "#f8fafc" }}
                            >
                              {claim.statement}
                            </strong>
                            <span
                              style={{
                                fontSize: "10px",
                                fontWeight: 700,
                                padding: "2px 6px",
                                borderRadius: "4px",
                                background: badge.background,
                                color: badge.color,
                              }}
                            >
                              {badge.label}
                            </span>
                          </div>
                          {claim.file_path && (
                            <div
                              style={{
                                fontSize: "12px",
                                color: "#94a3b8",
                                marginBottom: "4px",
                              }}
                            >
                              Source:{" "}
                              <code>
                                {claim.file_path}:{claim.line_start ?? 1}
                              </code>
                            </div>
                          )}
                          {claim.quoted_source && (
                            <pre
                              style={{
                                margin: "6px 0",
                                padding: "8px",
                                background: "#020617",
                                borderRadius: "4px",
                                fontSize: "11px",
                                color: "#38bdf8",
                                overflowX: "auto",
                              }}
                            >
                              {claim.quoted_source}
                            </pre>
                          )}
                          {claim.verification_notes && (
                            <div
                              style={{
                                fontSize: "11px",
                                color: "#cbd5e1",
                                fontStyle: "italic",
                              }}
                            >
                              Verification check: {claim.verification_notes}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Live Events Stream & Timeline */}
              <div>
                <h4
                  style={{
                    margin: "0 0 10px",
                    fontSize: "14px",
                    fontWeight: 600,
                    color: "#94a3b8",
                  }}
                >
                  Investigation Replay & Audit Trail ({liveEvents.length}{" "}
                  events)
                </h4>
                <div
                  id="events-timeline"
                  style={{
                    maxHeight: "180px",
                    overflowY: "auto",
                    padding: "8px",
                    background: "#0f172a",
                    borderRadius: "6px",
                    display: "flex",
                    flexDirection: "column",
                    gap: "6px",
                  }}
                >
                  {liveEvents.map((ev, i) => (
                    <div
                      key={ev.id || i}
                      style={{
                        fontSize: "11px",
                        display: "flex",
                        justifyContent: "space-between",
                        padding: "4px 8px",
                        borderRadius: "4px",
                        background: "#1e293b",
                      }}
                    >
                      <span style={{ color: "#38bdf8", fontWeight: 600 }}>
                        {ev.event_type}
                      </span>
                      <span style={{ color: "#94a3b8" }}>{ev.node_name}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Human Feedback & Verified Memory Persistence */}
              <div
                style={{
                  padding: "16px",
                  background: "#0f172a",
                  borderRadius: "8px",
                  border: "1px solid #334155",
                }}
              >
                <h4
                  style={{
                    margin: "0 0 10px",
                    fontSize: "13px",
                    fontWeight: 600,
                    color: "#f8fafc",
                  }}
                >
                  Human Review & Repository Memory
                </h4>
                <div
                  style={{ display: "flex", gap: "8px", marginBottom: "10px" }}
                >
                  <button
                    id="feedback-agree-btn"
                    type="button"
                    onClick={() => setFeedbackType("agree")}
                    style={{
                      padding: "6px 12px",
                      borderRadius: "4px",
                      border: "none",
                      background:
                        feedbackType === "agree" ? "#16a34a" : "#334155",
                      color: "#fff",
                      fontSize: "12px",
                      cursor: "pointer",
                    }}
                  >
                    Confirm (Agree)
                  </button>
                  <button
                    id="feedback-disagree-btn"
                    type="button"
                    onClick={() => setFeedbackType("disagree")}
                    style={{
                      padding: "6px 12px",
                      borderRadius: "4px",
                      border: "none",
                      background:
                        feedbackType === "disagree" ? "#dc2626" : "#334155",
                      color: "#fff",
                      fontSize: "12px",
                      cursor: "pointer",
                    }}
                  >
                    Reject (Disagree)
                  </button>
                  <button
                    id="feedback-needs-review-btn"
                    type="button"
                    onClick={() => setFeedbackType("needs_review")}
                    style={{
                      padding: "6px 12px",
                      borderRadius: "4px",
                      border: "none",
                      background:
                        feedbackType === "needs_review" ? "#d97706" : "#334155",
                      color: "#fff",
                      fontSize: "12px",
                      cursor: "pointer",
                    }}
                  >
                    Needs More Context
                  </button>
                </div>

                <textarea
                  id="feedback-notes-input"
                  value={feedbackNotes}
                  onChange={(e) => setFeedbackNotes(e.target.value)}
                  placeholder="Add architectural explanation or triage notes for memory persistence..."
                  rows={2}
                  style={{
                    width: "100%",
                    padding: "6px 8px",
                    borderRadius: "4px",
                    border: "1px solid #475569",
                    background: "#1e293b",
                    color: "#f8fafc",
                    fontSize: "12px",
                    boxSizing: "border-box",
                    marginBottom: "8px",
                  }}
                />

                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <label
                    style={{
                      fontSize: "12px",
                      color: "#94a3b8",
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                    }}
                  >
                    <input
                      id="save-to-memory-checkbox"
                      type="checkbox"
                      checked={saveToMemory}
                      onChange={(e) => setSaveToMemory(e.target.checked)}
                    />
                    Persist to Verified Security Memory for future
                    investigations
                  </label>

                  <button
                    id="submit-feedback-btn"
                    type="button"
                    disabled={
                      feedbackMutation.isPending || !feedbackNotes.trim()
                    }
                    onClick={() =>
                      feedbackMutation.mutate({
                        feedback_type: feedbackType,
                        notes: feedbackNotes.trim(),
                        save_to_memory: saveToMemory,
                      })
                    }
                    style={{
                      padding: "6px 14px",
                      borderRadius: "4px",
                      border: "none",
                      background: "#38bdf8",
                      color: "#0f172a",
                      fontWeight: 600,
                      fontSize: "12px",
                      cursor:
                        !feedbackNotes.trim() || feedbackMutation.isPending
                          ? "not-allowed"
                          : "pointer",
                      opacity:
                        !feedbackNotes.trim() || feedbackMutation.isPending
                          ? 0.6
                          : 1,
                    }}
                  >
                    {feedbackMutation.isPending
                      ? "Recording..."
                      : "Save Review"}
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div
              style={{
                padding: "48px 0",
                textAlign: "center",
                color: "#64748b",
              }}
            >
              Select an investigation from the left or launch a new hypothesis
              to view evidence details.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type {
  InvestigationClaimV1,
  InvestigationDetail,
  InvestigationEvent,
  InvestigationFeedbackRequestV1,
  InvestigationMode,
  InvestigationSummaryV2,
  InvestigationVerdict,
} from "./types";

interface ExtendedSummary {
  held_micros?: number;
  failure_reason?: string | null;
  current_job_id?: string | null;
  spec_hash?: string | null;
}

interface CitationItem {
  file_path?: string;
  path?: string;
  line_start?: number;
  line?: number;
  statement?: string;
  snippet?: string;
}

interface ExtendedReport {
  claims?: InvestigationClaimV1[];
  coverage_notes?: string[];
  citations?: CitationItem[];
}

interface ExtendedDetail {
  claims?: InvestigationClaimV1[];
  report?: ExtendedReport | null;
}

interface EventShape {
  id?: string;
  sequence?: number;
  event_type?: string;
  type?: string;
  node_name?: string;
  node?: string;
}

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
  const [liveEvents, setLiveEvents] = useState<InvestigationEvent[]>([]);

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
      const data = query.state.data as InvestigationDetail | null;
      const status = data?.summary?.status;
      if (!status) return false;
      return status === "running" || status === "pending" || status === "checkpointed" ? 2000 : false;
    },
  });

  // Query events list for selected investigation
  const eventsQuery = useQuery({
    queryKey: ["investigation-events", repositoryId, selectedInvId],
    queryFn: ({ signal }) =>
      selectedInvId
        ? api.listInvestigationEvents(repositoryId, selectedInvId, undefined, signal)
        : null,
    enabled: !!selectedInvId && !!repositoryId,
    refetchInterval: () => {
      const data = detailQuery.data;
      const status = data?.summary?.status;
      if (!status) return false;
      return status === "running" || status === "pending" || status === "checkpointed" ? 2000 : false;
    },
  });

  // Create mutation
  const createMutation = useMutation({
    mutationFn: async () => {
      const trimmedQuery = query.trim();
      const trimmedFindingId = findingId.trim() || undefined;
      const targetType = trimmedFindingId ? "finding" : "repository";

      if (snapshotId) {
        return await api.createInvestigation(
          repositoryId,
          {
            repository_id: repositoryId,
            snapshot_id: snapshotId,
            target_type: targetType,
            query: trimmedQuery,
            finding_id: trimmedFindingId,
            mode,
            max_steps: maxSteps,
            budget_limit_micros: budgetMicros,
          },
          csrfToken,
        );
      }
      return await api.createInvestigation(
        repositoryId,
        {
          repository_id: repositoryId,
          query: trimmedQuery,
          finding_id: trimmedFindingId,
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
      if ("summary" in result) {
        queryClient.setQueryData(
          ["investigation-detail", repositoryId, selectedInvId],
          result,
        );
      }
      queryClient.invalidateQueries({
        queryKey: ["investigation-detail", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigation-events", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
    },
  });

  // Cancel mutation
  const cancelMutation = useMutation({
    mutationFn: async (id: string) => {
      return await api.cancelInvestigation(repositoryId, id, csrfToken);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["investigation-detail", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigation-events", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
    },
  });

  // Resume mutation
  const resumeMutation = useMutation({
    mutationFn: async () => {
      if (!summary) return;
      const currentJobId = (summary as InvestigationSummaryV2).current_job_id ?? "";
      const specHash = (summary as InvestigationSummaryV2).spec_hash ?? "";
      return await api.resumeInvestigation(
        repositoryId,
        summary.id,
        {
          expected_failed_job_id: currentJobId,
          expected_spec_hash: specHash,
        },
        csrfToken,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["investigation-detail", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigation-events", repositoryId, selectedInvId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
    },
  });

  // Retry mutation
  const retryMutation = useMutation({
    mutationFn: async () => {
      if (!summary) return;
      return await api.retryInvestigation(
        repositoryId,
        summary.id,
        {
          max_steps: maxSteps,
          budget_limit_micros: budgetMicros,
        },
        csrfToken,
      );
    },
    onSuccess: (newSummary) => {
      if (newSummary?.id) {
        setSelectedInvId(newSummary.id);
      }
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigation-detail", repositoryId, newSummary?.id],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigation-events", repositoryId, newSummary?.id],
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
    if (!selectedInvId || typeof EventSource === "undefined") return;
    const sseUrl = `/api/v1/repositories/${repositoryId}/investigations/${selectedInvId}/stream`;
    const eventSource = new EventSource(sseUrl);

    eventSource.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as InvestigationEvent;
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
        const parsed = JSON.parse(e.data) as InvestigationEvent;
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
        queryKey: ["investigation-events", repositoryId, selectedInvId],
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
        className="investigation-heading"
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
        <div style={{ display: "flex", flexWrap: "wrap", gap: "12px", alignItems: "center" }}>
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
        className="investigation-columns"
        style={{
          display: "grid",
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
                className="resize-none"
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
                      {(inv.query || "").slice(0, 36)}...
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
                        : (inv.status || "").toUpperCase()}
                    </span>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: "11px",
                      color: "#94a3b8",
                      flexWrap: "wrap",
                      gap: "4px",
                    }}
                  >
                    <span>
                      Step: {inv.current_step ?? 0}/{inv.max_steps ?? 10}
                    </span>
                    <span>Spent: {formatMicros(inv.spent_micros ?? 0)}</span>
                    {(inv as unknown as ExtendedSummary).held_micros ? (
                      <span style={{ color: "#fbbf24" }}>
                        Held: {formatMicros((inv as unknown as ExtendedSummary).held_micros ?? 0)}
                      </span>
                    ) : null}
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
              {/* Detail Header & Actions */}
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "flex-start",
                  borderBottom: "1px solid #334155",
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
                            ? "#065f46"
                            : summary.status === "awaiting_human_decision"
                              ? "#78350f"
                              : summary.status === "failed" || summary.status === "cancelled"
                                ? "#7f1d1d"
                                : "#1e3a8a",
                        color:
                          summary.status === "completed"
                            ? "#34d399"
                            : summary.status === "awaiting_human_decision"
                              ? "#fde047"
                              : summary.status === "failed" || summary.status === "cancelled"
                                ? "#fca5a5"
                                : "#60a5fa",
                      }}
                    >
                      {summary.status.toUpperCase()}
                    </span>
                    <span style={{ fontSize: "12px", color: "#64748b" }}>
                      ID: {summary.id.slice(0, 8)}
                    </span>
                    {(summary as unknown as ExtendedSummary).current_job_id && (
                      <span style={{ fontSize: "11px", color: "#64748b" }}>
                        Job: {String((summary as unknown as ExtendedSummary).current_job_id).slice(0, 8)}
                      </span>
                    )}
                  </div>
                  <h4 style={{ margin: 0, fontSize: "16px", color: "#f8fafc" }}>
                    {summary.query}
                  </h4>
                </div>

                {/* Header Action Buttons */}
                <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
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
                        background: "#10b981",
                        color: "#ffffff",
                        fontWeight: 600,
                        fontSize: "13px",
                        cursor: executeMutation.isPending ? "not-allowed" : "pointer",
                      }}
                    >
                      {executeMutation.isPending ? "Starting..." : "Execute Investigation"}
                    </button>
                  )}

                  {(summary.status === "running" ||
                    summary.status === "pending" ||
                    summary.status === "queued" ||
                    summary.status === "checkpointed" ||
                    summary.status === "awaiting_human_decision") && (
                    <button
                      id="cancel-investigation-btn"
                      type="button"
                      disabled={cancelMutation.isPending}
                      onClick={() => cancelMutation.mutate(summary.id)}
                      style={{
                        padding: "8px 16px",
                        borderRadius: "6px",
                        border: "none",
                        background: "#ef4444",
                        color: "#ffffff",
                        fontWeight: 600,
                        fontSize: "13px",
                        cursor: cancelMutation.isPending ? "not-allowed" : "pointer",
                      }}
                    >
                      {cancelMutation.isPending ? "Cancelling..." : "Cancel"}
                    </button>
                  )}

                  {summary.status === "awaiting_human_decision" && (
                    <button
                      id="resume-investigation-btn"
                      type="button"
                      disabled={resumeMutation.isPending}
                      onClick={() => resumeMutation.mutate()}
                      style={{
                        padding: "8px 16px",
                        borderRadius: "6px",
                        border: "none",
                        background: "#f59e0b",
                        color: "#ffffff",
                        fontWeight: 600,
                        fontSize: "13px",
                        cursor: resumeMutation.isPending ? "not-allowed" : "pointer",
                      }}
                    >
                      {resumeMutation.isPending ? "Resuming..." : "Resume"}
                    </button>
                  )}

                  {(summary.status === "failed" || summary.status === "cancelled") && (
                    <button
                      id="retry-investigation-btn"
                      type="button"
                      disabled={retryMutation.isPending}
                      onClick={() => retryMutation.mutate()}
                      style={{
                        padding: "8px 16px",
                        borderRadius: "6px",
                        border: "none",
                        background: "#8b5cf6",
                        color: "#ffffff",
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

              {/* Human Decision Banner */}
              {summary.status === "awaiting_human_decision" && (
                <div
                  id="human-decision-banner"
                  style={{
                    padding: "14px",
                    borderRadius: "8px",
                    background: "#78350f",
                    border: "1px solid #f59e0b",
                    color: "#fef3c7",
                    fontSize: "13px",
                  }}
                >
                  <div style={{ fontWeight: 700, marginBottom: "4px" }}>
                    Human Decision Required
                  </div>
                  <div>
                    This investigation has reached a decision checkpoint that requires operator authorization or resolution before proceeding. Click Resume to continue execution.
                  </div>
                </div>
              )}

              {/* Failure Reason Banner */}
              {(summary as unknown as ExtendedSummary).failure_reason && (
                <div
                  id="failure-reason-banner"
                  style={{
                    padding: "12px",
                    borderRadius: "8px",
                    background: "#450a0a",
                    border: "1px solid #ef4444",
                    color: "#fecaca",
                    fontSize: "12px",
                  }}
                >
                  <strong>Failure Reason: </strong> {(summary as unknown as ExtendedSummary).failure_reason}
                </div>
              )}

              {/* Budget and Progress Summary Metrics */}
              <div
                id="investigation-metrics-grid"
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
                  gap: "12px",
                  padding: "12px",
                  background: "#0f172a",
                  borderRadius: "8px",
                  border: "1px solid #334155",
                }}
              >
                <div>
                  <div style={{ fontSize: "11px", color: "#94a3b8" }}>Step Progress</div>
                  <div style={{ fontSize: "14px", fontWeight: 700, color: "#f8fafc" }}>
                    {summary.current_step ?? 0} / {summary.max_steps ?? 10}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "11px", color: "#94a3b8" }}>Settled Spend</div>
                  <div id="metric-spent-micros" style={{ fontSize: "14px", fontWeight: 700, color: "#38bdf8" }}>
                    {formatMicros(summary.spent_micros ?? 0)}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "11px", color: "#94a3b8" }}>Held Budget</div>
                  <div
                    id="metric-held-micros"
                    style={{
                      fontSize: "14px",
                      fontWeight: 700,
                      color: (summary as unknown as ExtendedSummary).held_micros ? "#fbbf24" : "#94a3b8",
                    }}
                  >
                    {formatMicros((summary as unknown as ExtendedSummary).held_micros ?? 0)}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "11px", color: "#94a3b8" }}>Budget Limit</div>
                  <div style={{ fontSize: "14px", fontWeight: 700, color: "#a855f7" }}>
                    {formatMicros(summary.budget_limit_micros ?? 0)}
                  </div>
                </div>
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
              {(() => {
                const extDetail = detail as unknown as ExtendedDetail;
                const claims: InvestigationClaimV1[] = Array.isArray(extDetail?.claims)
                  ? extDetail.claims
                  : Array.isArray(extDetail?.report?.claims)
                    ? (extDetail.report.claims ?? [])
                    : [];

                const coverageNotes: string[] = Array.isArray(extDetail?.report?.coverage_notes)
                  ? (extDetail.report.coverage_notes ?? [])
                  : [];

                const citations: CitationItem[] = Array.isArray(extDetail?.report?.citations)
                  ? (extDetail.report.citations ?? [])
                  : [];

                return (
                  <div>
                    <h4
                      style={{
                        margin: "0 0 12px",
                        fontSize: "14px",
                        fontWeight: 600,
                        color: "#38bdf8",
                      }}
                    >
                      Evidence Claims ({claims.length})
                    </h4>
                    {claims.length === 0 ? (
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
                        {claims.map((claim) => {
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

                    {/* Coverage Notes */}
                    {coverageNotes.length > 0 && (
                      <div
                        id="coverage-notes-section"
                        style={{
                          marginTop: "16px",
                          padding: "12px",
                          background: "#0f172a",
                          borderRadius: "6px",
                          border: "1px solid #334155",
                        }}
                      >
                        <h5
                          style={{
                            margin: "0 0 8px",
                            fontSize: "12px",
                            color: "#38bdf8",
                            fontWeight: 600,
                          }}
                        >
                          Coverage Notes
                        </h5>
                        <ul
                          style={{
                            margin: 0,
                            paddingLeft: "18px",
                            fontSize: "12px",
                            color: "#cbd5e1",
                          }}
                        >
                          {coverageNotes.map((note, idx) => (
                            <li key={idx} style={{ marginBottom: "4px" }}>
                              {note}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Citations */}
                    {citations.length > 0 && (
                      <div
                        id="citations-section"
                        style={{
                          marginTop: "16px",
                          padding: "12px",
                          background: "#0f172a",
                          borderRadius: "6px",
                          border: "1px solid #334155",
                        }}
                      >
                        <h5
                          style={{
                            margin: "0 0 8px",
                            fontSize: "12px",
                            color: "#38bdf8",
                            fontWeight: 600,
                          }}
                        >
                          Citations & References ({citations.length})
                        </h5>
                        <div
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: "8px",
                          }}
                        >
                          {citations.map((c, idx) => (
                            <div
                              key={idx}
                              style={{
                                fontSize: "11px",
                                color: "#94a3b8",
                                padding: "6px",
                                background: "#020617",
                                borderRadius: "4px",
                              }}
                            >
                              <code>
                                {c.file_path || c.path || "source"}:
                                {c.line_start ?? c.line ?? 1}
                              </code>
                              {c.statement && (
                                <div style={{ color: "#f8fafc", marginTop: "2px" }}>
                                  {c.statement}
                                </div>
                              )}
                              {c.snippet && (
                                <pre
                                  style={{
                                    margin: "4px 0 0",
                                    padding: "4px 6px",
                                    background: "#0f172a",
                                    borderRadius: "3px",
                                    color: "#38bdf8",
                                    overflowX: "auto",
                                  }}
                                >
                                  {c.snippet}
                                </pre>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* Events Stream & Timeline */}
              {(() => {
                const historicalEvents: InvestigationEvent[] = Array.isArray(
                  eventsQuery.data,
                )
                  ? eventsQuery.data
                  : (eventsQuery.data?.items ?? []);

                const allEvents: InvestigationEvent[] = [...historicalEvents];
                for (const ev of liveEvents) {
                  if (
                    !allEvents.some(
                      (item) =>
                        (item.id && item.id === ev.id) ||
                        ("sequence" in item &&
                          "sequence" in ev &&
                          item.sequence === ev.sequence),
                    )
                  ) {
                    allEvents.push(ev);
                  }
                }

                return (
                  <div>
                    <h4
                      style={{
                        margin: "0 0 10px",
                        fontSize: "14px",
                        fontWeight: 600,
                        color: "#94a3b8",
                      }}
                    >
                      Investigation Replay & Audit Trail ({allEvents.length} events)
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
                      {allEvents.length === 0 ? (
                        <div
                          style={{
                            fontSize: "12px",
                            color: "#64748b",
                            fontStyle: "italic",
                            padding: "4px",
                          }}
                        >
                          No replay events recorded yet.
                        </div>
                      ) : (
                        allEvents.map((ev, i) => {
                          const evShape = ev as unknown as EventShape;
                          const evKey =
                            "sequence" in ev && ev.sequence !== undefined
                              ? `seq-${ev.sequence}`
                              : evShape.id || i;
                          const eventType =
                            evShape.event_type ||
                            evShape.type ||
                            "event";
                          const nodeName =
                            evShape.node_name ||
                            evShape.node ||
                            "";
                          const seqText =
                            "sequence" in ev && ev.sequence !== undefined
                              ? `#${ev.sequence} `
                              : "";

                          return (
                            <div
                              key={evKey}
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
                                {seqText}
                                {eventType}
                              </span>
                              <span style={{ color: "#94a3b8" }}>{nodeName}</span>
                            </div>
                          );
                        })
                      )}
                    </div>
                  </div>
                );
              })()}

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
                  className="resize-none"
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

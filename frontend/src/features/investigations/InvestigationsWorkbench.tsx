import {
  selectedInvestigation,
  rememberInvestigation,
} from "../../workspace/location";
import { reportClaims, reportCaveats, type ClaimView } from "./reportView";
import { Modal } from "../../ui/Modal";
import { useEffect, useRef, useState } from "react";
import { ModelAvailabilityPanel } from "./ModelAvailabilityPanel";
import { InlineNotice } from "../../ui/InlineNotice";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
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
  findingRunId?: string;
  csrfToken: string;
  onFindingTriageRequested?: (findingId: string) => void;
}

function operationProblem(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401)
      return "Your session expired. Sign in again before continuing.";
    if (error.status === 403)
      return "Access changed. Reload your repository permissions.";
    if (error.status === 409)
      return "The investigation changed. Reload its status before retrying.";
    if (error.status === 429)
      return "A usage limit was reached. Check the budget and try again later.";
    if (error.status === 503)
      return "Investigation execution is unavailable. Check the configured worker and model runtime.";
  }
  return "The request could not be confirmed. Retry the same action to check its outcome.";
}

export function InvestigationsWorkbench({
  repositoryId,
  snapshotId,
  findingId: initialFindingId,
  findingRunId: initialFindingRunId,
  csrfToken,
}: Props) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const targetKey = `${initialFindingId ?? ""}:${initialFindingRunId ?? ""}`;
  const [findingOverride, setFindingOverride] = useState<{
    targetKey: string;
    id: string;
    run: string;
  }>();
  const findingId =
    findingOverride?.targetKey === targetKey
      ? findingOverride.id
      : (initialFindingId ?? "");
  const findingRunId =
    findingOverride?.targetKey === targetKey
      ? findingOverride.run
      : (initialFindingRunId ?? "");
  const setFindingId = (id: string) =>
    setFindingOverride({
      targetKey,
      id,
      run: id === findingId ? findingRunId : "",
    });
  const setFindingRunId = (run: string) =>
    setFindingOverride({ targetKey, id: findingId, run });
  const uuid =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const invalidFinding =
    !!findingId.trim() &&
    (!uuid.test(findingId.trim()) ||
      (!!snapshotId && !uuid.test(findingRunId.trim())));
  const [mode, setMode] = useState<InvestigationMode>("short_path");
  const [maxSteps, setMaxSteps] = useState(10);
  const [budgetMicros, setBudgetMicros] = useState(5_000_000); // $5.00
  const [selectedInvId, setSelection] = useState<string | null>(() =>
    selectedInvestigation(repositoryId, snapshotId),
  );
  const [feedbackNotes, setFeedbackNotes] = useState("");
  const [feedbackType, setFeedbackType] = useState<
    "agree" | "disagree" | "needs_review"
  >("agree");
  const [saveToMemory, setSaveToMemory] = useState(true);
  const [cancelIntent, setCancelIntent] = useState<string | null>(null);
  const [liveEvents, setLiveEvents] = useState<InvestigationEvent[]>([]);
  const [eventCursor, setEventCursor] = useState<number | undefined>();
  const [historyCursor, setHistoryCursor] = useState<string | undefined>();
  const commandKeys = useRef(
    new Map<string, { signature: string; key: string }>(),
  );
  function commandKey(operation: string, payload: unknown) {
    const signature = JSON.stringify(payload);
    if (commandKeys.current.get(operation)?.signature !== signature)
      commandKeys.current.set(operation, {
        signature,
        key: crypto.randomUUID(),
      });
    return commandKeys.current.get(operation)!.key;
  }

  function setSelectedInvId(id: string) {
    setEventCursor(undefined);
    setSelection(id);
    rememberInvestigation(id);
    setLiveEvents([]);
    setFeedbackNotes("");
    setCancelIntent(null);
  }
  useEffect(() => {
    const restore = () => {
      const next = selectedInvestigation(repositoryId, snapshotId);
      if (next === selectedInvId) return;
      setEventCursor(undefined);
      setSelection(next);
      setLiveEvents([]);
      setFeedbackNotes("");
      setCancelIntent(null);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [repositoryId, snapshotId, selectedInvId]);

  // List past investigations
  const listQuery = useQuery({
    queryKey: ["investigations", repositoryId, historyCursor],
    queryFn: ({ signal }) =>
      api.listInvestigations(repositoryId, undefined, signal, historyCursor),
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
      return status === "running" ||
        status === "pending" ||
        status === "checkpointed"
        ? 2000
        : false;
    },
  });

  // Query events list for selected investigation
  const eventsQuery = useQuery({
    queryKey: [
      "investigation-events",
      repositoryId,
      selectedInvId,
      eventCursor,
    ],
    queryFn: ({ signal }) =>
      selectedInvId
        ? api.listInvestigationEvents(
            repositoryId,
            selectedInvId,
            eventCursor,
            signal,
          )
        : null,
    enabled: !!selectedInvId && !!repositoryId,
    refetchInterval: () => {
      const data = detailQuery.data;
      const status = data?.summary?.status;
      if (!status) return false;
      return status === "running" ||
        status === "pending" ||
        status === "checkpointed"
        ? 2000
        : false;
    },
  });

  // Create mutation
  const createMutation = useMutation({
    mutationFn: async () => {
      const trimmedQuery = query.trim();
      const trimmedFindingId = findingId.trim() || undefined;
      const targetType = trimmedFindingId ? "finding" : "repository";
      const key = commandKey("create", [
        repositoryId,
        snapshotId,
        trimmedQuery,
        trimmedFindingId,
        findingRunId,
        mode,
        maxSteps,
        budgetMicros,
      ]);

      if (snapshotId) {
        return await api.createInvestigation(
          repositoryId,
          {
            repository_id: repositoryId,
            snapshot_id: snapshotId,
            target_type: targetType,
            query: trimmedQuery,
            finding_id: trimmedFindingId,
            finding_run_id: trimmedFindingId
              ? findingRunId.trim() || undefined
              : undefined,
            mode,
            max_steps: maxSteps,
            budget_limit_micros: budgetMicros,
          },
          csrfToken,
          key,
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
        key,
      );
    },
    onSuccess: (newInv) => {
      commandKeys.current.delete("create");
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
      return await api.executeInvestigation(
        repositoryId,
        id,
        csrfToken,
        commandKey("execute", [repositoryId, id]),
      );
    },
    onSuccess: (result, submittedId) => {
      if ("summary" in result) {
        queryClient.setQueryData(
          ["investigation-detail", repositoryId, submittedId],
          result,
        );
      }
      queryClient.invalidateQueries({
        queryKey: ["investigation-detail", repositoryId, submittedId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigation-events", repositoryId, submittedId],
      });
      queryClient.invalidateQueries({
        queryKey: ["investigations", repositoryId],
      });
    },
  });

  // Cancel mutation
  const cancelMutation = useMutation({
    mutationFn: async (id: string) => {
      return await api.cancelInvestigation(
        repositoryId,
        id,
        csrfToken,
        commandKey("cancel", [repositoryId, id]),
      );
    },
    onSuccess: () => {
      setCancelIntent(null);
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
      const currentJobId =
        (summary as InvestigationSummaryV2).current_job_id ?? "";
      const specHash = (summary as InvestigationSummaryV2).spec_hash ?? "";
      return await api.resumeInvestigation(
        repositoryId,
        summary.id,
        {
          expected_failed_job_id: currentJobId,
          expected_spec_hash: specHash,
        },
        csrfToken,
        commandKey("resume", [
          repositoryId,
          summary.id,
          currentJobId,
          specHash,
        ]),
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
        commandKey("retry", [repositoryId, summary.id, maxSteps, budgetMicros]),
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
        commandKey("feedback", [repositoryId, selectedInvId, fb]),
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
          return [...prev, parsed].slice(-200);
        });
      } catch {
        // Ignored unparsable SSE ping
      }
    };

    eventSource.addEventListener("step_started", (e: MessageEvent) => {
      try {
        const parsed = JSON.parse(e.data) as InvestigationEvent;
        setLiveEvents((prev) => [...prev, parsed].slice(-200));
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
  const actionAvailable = (action: string, legacy: boolean) =>
    detail && "available_actions" in detail
      ? (detail.available_actions ?? []).includes(action)
      : legacy;

  const formatMicros = (micros: number) => {
    return `$${(micros / 1_000_000).toFixed(4)}`;
  };

  const getVerdictStyle = (verdict?: InvestigationVerdict | null) => {
    switch (verdict) {
      case "true_positive":
        return {
          background: "var(--cs-danger-bg)",
          color: "var(--cs-danger)",
          border: "1px solid var(--cs-danger)",
        };
      case "false_positive":
        return {
          background: "var(--cs-success-bg)",
          color: "var(--cs-success)",
          border: "1px solid var(--cs-success)",
        };
      case "benign":
        return {
          background: "var(--cs-info-bg)",
          color: "var(--cs-action)",
          border: "1px solid var(--cs-action)",
        };
      default:
        return {
          background: "var(--cs-warning-bg)",
          color: "var(--cs-warning)",
          border: "1px solid var(--cs-warning)",
        };
    }
  };

  const getClaimBadgeStyle = (status: ClaimView["status"]) => {
    switch (status) {
      case "source_correspondence_confirmed":
      case "verified":
        return {
          background: "var(--cs-success-bg)",
          color: "var(--cs-success)",
          label: "SOURCE CORRESPONDENCE (AT PUBLICATION)",
        };
      case "refuted":
        return {
          background: "var(--cs-danger-bg)",
          color: "var(--cs-danger)",
          label: "REFUTED CITATION",
        };
      case "location_valid_quote_missing":
      case "evidence_unavailable":
      case "unsupported":
        return {
          background: "var(--cs-warning-bg)",
          color: "var(--cs-warning)",
          label: status.replaceAll("_", " ").toUpperCase(),
        };
      default:
        return {
          background: "var(--cs-raised)",
          color: "var(--cs-muted)",
          label: "PENDING",
        };
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
        background: "var(--cs-panel)",
        color: "var(--cs-text)",
        borderRadius: "12px",
        fontFamily: "var(--cs-font)",
      }}
    >
      <Modal
        open={!!cancelIntent}
        title="Cancel investigation"
        onClose={() => setCancelIntent(null)}
        busy={cancelMutation.isPending}
        description="This stops further work. A model call already sent may still settle usage."
      >
        <button
          type="button"
          disabled={cancelMutation.isPending}
          onClick={() => cancelMutation.mutate(cancelIntent!)}
        >
          Confirm cancellation
        </button>
        <button
          type="button"
          disabled={cancelMutation.isPending}
          onClick={() => setCancelIntent(null)}
        >
          Keep running
        </button>
      </Modal>
      <ModelAvailabilityPanel repositoryId={repositoryId} />
      {[
        createMutation,
        executeMutation,
        cancelMutation,
        resumeMutation,
        retryMutation,
        feedbackMutation,
      ]
        .filter((mutation) => mutation.isError)
        .map((mutation, index) => (
          <InlineNotice key={index} tone="danger">
            {operationProblem(mutation.error)}
          </InlineNotice>
        ))}
      {listQuery.isError && (
        <InlineNotice tone="danger">
          Investigations could not be loaded.{" "}
          <button type="button" onClick={() => void listQuery.refetch()}>
            Retry investigations
          </button>
        </InlineNotice>
      )}
      {detailQuery.isError && (
        <InlineNotice tone="danger">
          {operationProblem(detailQuery.error)}{" "}
          <button type="button" onClick={() => void detailQuery.refetch()}>
            Reload investigation
          </button>
        </InlineNotice>
      )}
      {eventsQuery.isError && (
        <InlineNotice tone="danger">
          The event timeline could not be refreshed.{" "}
          <button type="button" onClick={() => void eventsQuery.refetch()}>
            Retry timeline
          </button>
        </InlineNotice>
      )}
      {/* Header Banner */}
      <div
        className="investigation-heading"
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          borderBottom: "1px solid var(--cs-border)",
          paddingBottom: "16px",
        }}
      >
        <div>
          <h2
            id="workbench-title"
            tabIndex={-1}
            style={{
              margin: 0,
              fontSize: "20px",
              fontWeight: 700,
              color: "var(--cs-action)",
            }}
          >
            Investigation workbench
          </h2>
          <p
            style={{
              margin: "4px 0 0",
              fontSize: "13px",
              color: "var(--cs-muted)",
            }}
          >
            Investigate questions with source evidence, bounded tools and saved
            results.
          </p>
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "12px",
            alignItems: "center",
          }}
        >
          <div
            style={{
              padding: "6px 12px",
              background: "var(--cs-raised)",
              borderRadius: "6px",
              fontSize: "12px",
              border: "1px solid var(--cs-border)",
            }}
          >
            <span style={{ color: "var(--cs-muted)" }}>Mode: </span>
            <strong style={{ color: "var(--cs-action)" }}>
              {mode === "short_path" ? "Short Path" : "Two Specialist"}
            </strong>
          </div>
          {snapshotId && (
            <div
              style={{
                padding: "6px 12px",
                background: "var(--cs-raised)",
                borderRadius: "6px",
                fontSize: "12px",
                border: "1px solid var(--cs-border)",
              }}
            >
              <span style={{ color: "var(--cs-muted)" }}>Snapshot: </span>
              <strong style={{ color: "var(--cs-text)" }}>
                {snapshotId.slice(0, 8)}
              </strong>
            </div>
          )}
          <div
            style={{
              padding: "6px 12px",
              background: "var(--cs-raised)",
              borderRadius: "6px",
              fontSize: "12px",
              border: "1px solid var(--cs-border)",
            }}
          >
            <span style={{ color: "var(--cs-muted)" }}>Budget Limit: </span>
            <strong style={{ color: "var(--cs-action)" }}>
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
              background: "var(--cs-raised)",
              borderRadius: "8px",
              border: "1px solid var(--cs-border)",
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
                color: "var(--cs-text)",
              }}
            >
              New investigation
            </h3>

            <div>
              <label
                htmlFor="investigation-query-input"
                style={{
                  display: "block",
                  fontSize: "12px",
                  color: "var(--cs-muted)",
                  marginBottom: "4px",
                }}
              >
                Question or Finding to Investigate:
              </label>
              <textarea
                className="resize-none"
                id="investigation-query-input"
                value={query}
                maxLength={4000}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="e.g. Verify potential hardcoded API key in auth handler or validate SSRF path"
                rows={3}
                style={{
                  width: "100%",
                  padding: "8px",
                  borderRadius: "6px",
                  border: "1px solid var(--cs-control-border)",
                  background: "var(--cs-panel)",
                  color: "var(--cs-text)",
                  fontSize: "13px",
                  boxSizing: "border-box",
                }}
              />
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns:
                  "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
                gap: "12px",
              }}
            >
              <div>
                <label
                  htmlFor="investigation-mode-select"
                  style={{
                    display: "block",
                    fontSize: "12px",
                    color: "var(--cs-muted)",
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
                    border: "1px solid var(--cs-control-border)",
                    background: "var(--cs-panel)",
                    color: "var(--cs-text)",
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
                    color: "var(--cs-muted)",
                    marginBottom: "4px",
                  }}
                >
                  Target Finding ID (Optional):
                </label>
                {findingId.trim() && (
                  <>
                    <label htmlFor="finding-scan-run">
                      Finding scan run ID
                    </label>
                    <input
                      id="finding-scan-run"
                      value={findingRunId}
                      onChange={(event) => setFindingRunId(event.target.value)}
                      aria-invalid={invalidFinding}
                      aria-describedby="finding-binding-hint"
                    />
                    <p
                      id="finding-binding-hint"
                      className={invalidFinding ? "error" : "muted"}
                    >
                      Provide the exact finding and published scan run UUIDs.
                      Use “Investigate this finding” from a finding's detail
                      view to fill them automatically.
                    </p>
                  </>
                )}
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
                    border: "1px solid var(--cs-control-border)",
                    background: "var(--cs-panel)",
                    color: "var(--cs-text)",
                    fontSize: "12px",
                    boxSizing: "border-box",
                  }}
                />
              </div>
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns:
                  "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
                gap: "12px",
              }}
            >
              <div>
                <label
                  htmlFor="investigation-budget-select"
                  style={{
                    display: "block",
                    fontSize: "12px",
                    color: "var(--cs-muted)",
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
                    border: "1px solid var(--cs-control-border)",
                    background: "var(--cs-panel)",
                    color: "var(--cs-text)",
                    fontSize: "12px",
                  }}
                >
                  <option value={250_000}>$0.25 (Test)</option>
                  <option value={1_000_000}>$1.00 (Standard)</option>
                  <option value={5_000_000}>$5.00</option>
                  <option value={10_000_000}>$10.00 (Comprehensive)</option>
                </select>
              </div>

              <div>
                <label
                  htmlFor="investigation-max-steps-input"
                  style={{
                    display: "block",
                    fontSize: "12px",
                    color: "var(--cs-muted)",
                    marginBottom: "4px",
                  }}
                >
                  Maximum investigation steps:
                </label>
                <input
                  id="investigation-max-steps-input"
                  aria-label="Maximum investigation steps"
                  type="number"
                  min={1}
                  max={25}
                  value={maxSteps}
                  onChange={(e) => setMaxSteps(Number(e.target.value))}
                  style={{
                    width: "100%",
                    padding: "6px 8px",
                    borderRadius: "6px",
                    border: "1px solid var(--cs-control-border)",
                    background: "var(--cs-panel)",
                    color: "var(--cs-text)",
                    fontSize: "12px",
                    boxSizing: "border-box",
                  }}
                />
              </div>
            </div>

            <button
              id="start-investigation-btn"
              type="button"
              disabled={
                createMutation.isPending ||
                !query.trim() ||
                !Number.isInteger(maxSteps) ||
                maxSteps < 1 ||
                maxSteps > 25 ||
                invalidFinding
              }
              onClick={() => createMutation.mutate()}
              style={{
                marginTop: "8px",
                padding: "8px 16px",
                borderRadius: "6px",
                border: "none",
                background: "var(--cs-action)",
                color: "var(--cs-action-ink)",
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
                : "Create investigation"}
            </button>
            <p className="muted">
              Creation saves the request. Execute starts budgeted investigation
              work.
            </p>
          </div>

          {/* Past Investigations List */}
          <div
            style={{
              padding: "16px",
              background: "var(--cs-raised)",
              borderRadius: "8px",
              border: "1px solid var(--cs-border)",
            }}
          >
            <h3
              style={{
                margin: "0 0 12px",
                fontSize: "14px",
                fontWeight: 600,
                color: "var(--cs-text)",
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
              <div className="session-actions">
                {historyCursor && (
                  <button
                    type="button"
                    onClick={() => setHistoryCursor(undefined)}
                  >
                    Newest investigations
                  </button>
                )}
                {listQuery.data &&
                  "next_cursor" in listQuery.data &&
                  listQuery.data.next_cursor && (
                    <button
                      type="button"
                      disabled={listQuery.isFetching}
                      onClick={() =>
                        setHistoryCursor(
                          listQuery.data && "next_cursor" in listQuery.data
                            ? (listQuery.data.next_cursor ?? undefined)
                            : undefined,
                        )
                      }
                    >
                      Older investigations
                    </button>
                  )}
              </div>
              {listQuery.data?.items.map((inv) => (
                <button
                  type="button"
                  aria-pressed={selectedInvId === inv.id}
                  key={inv.id}
                  onClick={() => setSelectedInvId(inv.id)}
                  style={{
                    padding: "10px",
                    borderRadius: "6px",
                    background:
                      selectedInvId === inv.id
                        ? "var(--cs-raised)"
                        : "var(--cs-panel)",
                    border: `1px solid ${selectedInvId === inv.id ? "var(--cs-action)" : "var(--cs-raised)"}`,
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
                        color: "var(--cs-action)",
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
                      color: "var(--cs-muted)",
                      flexWrap: "wrap",
                      gap: "4px",
                    }}
                  >
                    <span>
                      Step: {inv.current_step ?? 0}/{inv.max_steps ?? 10}
                    </span>
                    <span>Spent: {formatMicros(inv.spent_micros ?? 0)}</span>
                    {(inv as unknown as ExtendedSummary).held_micros ? (
                      <span style={{ color: "var(--cs-warning)" }}>
                        Held:{" "}
                        {formatMicros(
                          (inv as unknown as ExtendedSummary).held_micros ?? 0,
                        )}
                      </span>
                    ) : null}
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Right Column: Active Investigation Details & Execution */}
        <div
          style={{
            padding: "20px",
            background: "var(--cs-raised)",
            borderRadius: "8px",
            border: "1px solid var(--cs-border)",
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
                    <span
                      style={{ fontSize: "12px", color: "var(--cs-muted)" }}
                    >
                      ID: {summary.id.slice(0, 8)}
                    </span>
                    {(summary as unknown as ExtendedSummary).current_job_id && (
                      <span
                        style={{ fontSize: "11px", color: "var(--cs-muted)" }}
                      >
                        Job:{" "}
                        {String(
                          (summary as unknown as ExtendedSummary)
                            .current_job_id,
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
                  {(summary.status === "pending" ||
                    summary.status === "queued") && (
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
                        cursor: executeMutation.isPending
                          ? "not-allowed"
                          : "pointer",
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
                        cursor: cancelMutation.isPending
                          ? "not-allowed"
                          : "pointer",
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
                        cursor: resumeMutation.isPending
                          ? "not-allowed"
                          : "pointer",
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
                        cursor: retryMutation.isPending
                          ? "not-allowed"
                          : "pointer",
                      }}
                    >
                      {retryMutation.isPending
                        ? "Retrying..."
                        : "Retry Investigation"}
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
                    background: "var(--cs-warning-bg)",
                    border: "1px solid var(--cs-warning)",
                    color: "var(--cs-warning)",
                    fontSize: "13px",
                  }}
                >
                  <div style={{ fontWeight: 700, marginBottom: "4px" }}>
                    Human Decision Required
                  </div>
                  <div>
                    This investigation has reached a decision checkpoint that
                    requires operator authorization or resolution before
                    proceeding. Click Resume to continue execution.
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
                    background: "var(--cs-danger-bg)",
                    border: "1px solid var(--cs-danger)",
                    color: "var(--cs-danger)",
                    fontSize: "12px",
                  }}
                >
                  <strong>Failure Reason: </strong>{" "}
                  {(summary as unknown as ExtendedSummary).failure_reason}
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
                  background: "var(--cs-panel)",
                  borderRadius: "8px",
                  border: "1px solid var(--cs-border)",
                }}
              >
                <div>
                  <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
                    Step Progress
                  </div>
                  <div
                    style={{
                      fontSize: "14px",
                      fontWeight: 700,
                      color: "var(--cs-text)",
                    }}
                  >
                    {summary.current_step ?? 0} / {summary.max_steps ?? 10}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
                    Settled Spend
                  </div>
                  <div
                    id="metric-spent-micros"
                    style={{
                      fontSize: "14px",
                      fontWeight: 700,
                      color: "var(--cs-action)",
                    }}
                  >
                    {formatMicros(summary.spent_micros ?? 0)}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
                    Held Budget
                  </div>
                  <div
                    id="metric-held-micros"
                    style={{
                      fontSize: "14px",
                      fontWeight: 700,
                      color: (summary as unknown as ExtendedSummary).held_micros
                        ? "var(--cs-warning)"
                        : "var(--cs-muted)",
                    }}
                  >
                    {formatMicros(
                      (summary as unknown as ExtendedSummary).held_micros ?? 0,
                    )}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
                    Budget Limit
                  </div>
                  <div
                    style={{
                      fontSize: "14px",
                      fontWeight: 700,
                      color: "var(--cs-action)",
                    }}
                  >
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
                        Reported confidence:{" "}
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
                const claims: ClaimView[] = Array.isArray(extDetail?.claims)
                  ? extDetail.claims
                  : reportClaims(
                      detail && "report" in detail ? detail.report : undefined,
                      !("current_support_available" in (detail ?? {})) ||
                        (detail &&
                          "current_support_available" in detail &&
                          detail.current_support_available === true),
                    );

                const coverageNotes: string[] = Array.isArray(
                  extDetail?.report?.coverage_notes,
                )
                  ? (extDetail.report.coverage_notes ?? [])
                  : typeof extDetail?.report?.coverage_notes === "string"
                    ? [extDetail.report.coverage_notes]
                    : [];

                const evidenceRefs =
                  detail &&
                  "report" in detail &&
                  Array.isArray(detail.report?.evidence_refs)
                    ? detail.report.evidence_refs.filter(
                        (value): value is string => typeof value === "string",
                      )
                    : [];

                const citations: CitationItem[] = Array.isArray(
                  extDetail?.report?.citations,
                )
                  ? (extDetail.report.citations ?? [])
                  : [];

                return (
                  <div>
                    <p className="muted">
                      Source correspondence checks a citation against retained
                      evidence at publication. It does not establish that a
                      security conclusion is true or remains current.
                    </p>
                    <h4
                      style={{
                        margin: "0 0 12px",
                        fontSize: "14px",
                        fontWeight: 600,
                        color: "var(--cs-action)",
                      }}
                    >
                      Evidence Claims ({claims.length})
                    </h4>
                    {claims.length === 0 ? (
                      <p
                        style={{
                          fontSize: "13px",
                          color: "var(--cs-muted)",
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
                                background: "var(--cs-panel)",
                                borderRadius: "6px",
                                border: "1px solid var(--cs-border)",
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
                                  style={{
                                    fontSize: "13px",
                                    color: "var(--cs-text)",
                                  }}
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
                                    color: "var(--cs-muted)",
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
                                    background: "var(--cs-canvas)",
                                    borderRadius: "4px",
                                    fontSize: "11px",
                                    color: "var(--cs-action)",
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
                                    color: "var(--cs-muted)",
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
                          background: "var(--cs-panel)",
                          borderRadius: "6px",
                          border: "1px solid var(--cs-border)",
                        }}
                      >
                        <h5
                          style={{
                            margin: "0 0 8px",
                            fontSize: "12px",
                            color: "var(--cs-action)",
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
                            color: "var(--cs-muted)",
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

                    {reportCaveats(
                      detail && "report" in detail ? detail.report : undefined,
                    ).map((group) => (
                      <section key={group.label} aria-label={group.label}>
                        <h4>{group.label}</h4>
                        <ul>
                          {group.items.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      </section>
                    ))}
                    {evidenceRefs.length > 0 && (
                      <div>
                        <h4>Evidence references ({evidenceRefs.length})</h4>
                        <ul>
                          {evidenceRefs.map((id) => (
                            <li key={id}>
                              <code>{id}</code>
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
                          background: "var(--cs-panel)",
                          borderRadius: "6px",
                          border: "1px solid var(--cs-border)",
                        }}
                      >
                        <h5
                          style={{
                            margin: "0 0 8px",
                            fontSize: "12px",
                            color: "var(--cs-action)",
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
                                color: "var(--cs-muted)",
                                padding: "6px",
                                background: "var(--cs-canvas)",
                                borderRadius: "4px",
                              }}
                            >
                              <code>
                                {c.file_path || c.path || "source"}:
                                {c.line_start ?? c.line ?? 1}
                              </code>
                              {c.statement && (
                                <div
                                  style={{
                                    color: "var(--cs-text)",
                                    marginTop: "2px",
                                  }}
                                >
                                  {c.statement}
                                </div>
                              )}
                              {c.snippet && (
                                <pre
                                  style={{
                                    margin: "4px 0 0",
                                    padding: "4px 6px",
                                    background: "var(--cs-panel)",
                                    borderRadius: "3px",
                                    color: "var(--cs-action)",
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
                for (const ev of eventCursor === undefined ? liveEvents : []) {
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
                        color: "var(--cs-muted)",
                      }}
                    >
                      Investigation Replay & Audit Trail ({allEvents.length}{" "}
                      events)
                    </h4>
                    <p className="muted">
                      This view shows one saved event page and up to 200 recent
                      live events. It is not the complete audit history.
                    </p>
                    <div className="session-actions">
                      {eventCursor !== undefined && (
                        <button
                          type="button"
                          onClick={() => setEventCursor(undefined)}
                        >
                          First event page
                        </button>
                      )}
                      {eventsQuery.data &&
                        !Array.isArray(eventsQuery.data) &&
                        eventsQuery.data.next_cursor != null && (
                          <button
                            type="button"
                            disabled={eventsQuery.isFetching}
                            onClick={() =>
                              setEventCursor(
                                eventsQuery.data &&
                                  !Array.isArray(eventsQuery.data)
                                  ? (eventsQuery.data.next_cursor ?? undefined)
                                  : undefined,
                              )
                            }
                          >
                            Next event page
                          </button>
                        )}
                    </div>
                    <div
                      id="events-timeline"
                      style={{
                        maxHeight: "180px",
                        overflowY: "auto",
                        padding: "8px",
                        background: "var(--cs-panel)",
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
                            color: "var(--cs-muted)",
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
                            evShape.event_type || evShape.type || "event";
                          const nodeName =
                            evShape.node_name || evShape.node || "";
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
                                background: "var(--cs-raised)",
                              }}
                            >
                              <span
                                style={{
                                  color: "var(--cs-action)",
                                  fontWeight: 600,
                                }}
                              >
                                {seqText}
                                {eventType}
                              </span>
                              <span style={{ color: "var(--cs-muted)" }}>
                                {nodeName}
                              </span>
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
                  background: "var(--cs-panel)",
                  borderRadius: "8px",
                  border: "1px solid var(--cs-border)",
                }}
              >
                <h4
                  style={{
                    margin: "0 0 10px",
                    fontSize: "13px",
                    fontWeight: 600,
                    color: "var(--cs-text)",
                  }}
                >
                  Human Review & Repository Memory
                </h4>
                <div
                  className="feedback-actions"
                  style={{ display: "flex", gap: "8px", marginBottom: "10px" }}
                >
                  <button
                    id="feedback-agree-btn"
                    aria-pressed={feedbackType === "agree"}
                    type="button"
                    onClick={() => setFeedbackType("agree")}
                    style={{
                      padding: "6px 12px",
                      borderRadius: "4px",
                      border: "none",
                      background:
                        feedbackType === "agree"
                          ? "var(--cs-success-bg)"
                          : "var(--cs-raised)",
                      color: "var(--cs-text)",
                      fontSize: "12px",
                      cursor: "pointer",
                    }}
                  >
                    Confirm (Agree)
                  </button>
                  <button
                    id="feedback-disagree-btn"
                    aria-pressed={feedbackType === "disagree"}
                    type="button"
                    onClick={() => setFeedbackType("disagree")}
                    style={{
                      padding: "6px 12px",
                      borderRadius: "4px",
                      border: "none",
                      background:
                        feedbackType === "disagree"
                          ? "var(--cs-danger-bg)"
                          : "var(--cs-raised)",
                      color: "var(--cs-text)",
                      fontSize: "12px",
                      cursor: "pointer",
                    }}
                  >
                    Reject (Disagree)
                  </button>
                  <button
                    id="feedback-needs-review-btn"
                    aria-pressed={feedbackType === "needs_review"}
                    type="button"
                    onClick={() => setFeedbackType("needs_review")}
                    style={{
                      padding: "6px 12px",
                      borderRadius: "4px",
                      border: "none",
                      background:
                        feedbackType === "needs_review"
                          ? "var(--cs-warning-bg)"
                          : "var(--cs-raised)",
                      color: "var(--cs-text)",
                      fontSize: "12px",
                      cursor: "pointer",
                    }}
                  >
                    Needs More Context
                  </button>
                </div>

                <label htmlFor="feedback-notes-input">Review notes</label>
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
                    border: "1px solid var(--cs-control-border)",
                    background: "var(--cs-raised)",
                    color: "var(--cs-text)",
                    fontSize: "12px",
                    boxSizing: "border-box",
                    marginBottom: "8px",
                  }}
                />

                <div
                  className="feedback-actions"
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <label
                    style={{
                      fontSize: "12px",
                      color: "var(--cs-muted)",
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
                    Propose a memory for human review
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
                      background: "var(--cs-action)",
                      color: "var(--cs-action-ink)",
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
                color: "var(--cs-muted)",
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

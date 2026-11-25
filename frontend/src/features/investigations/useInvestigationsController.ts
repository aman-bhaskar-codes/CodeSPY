import {
  selectedInvestigation,
  rememberInvestigation,
} from "../../workspace/location";
import { type ClaimView } from "./reportView";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type {
  InvestigationDetail,
  InvestigationEvent,
  InvestigationFeedbackRequestV1,
  InvestigationMode,
  InvestigationSummaryV2,
  InvestigationVerdict,
} from "./types";
import type { Props } from "./investigationTypes";

/** All state, queries and commands of the investigations workbench. Parts read it through context. */
export function useInvestigationsController(props: Props) {
  const {
    repositoryId,
    snapshotId,
    findingId: initialFindingId,
    findingRunId: initialFindingRunId,
    csrfToken,
  } = props;
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
  const commandKeysRef = useRef(
    new Map<string, { signature: string; key: string }>(),
  );
  function commandKey(operation: string, payload: unknown) {
    const signature = JSON.stringify(payload);
    if (commandKeysRef.current.get(operation)?.signature !== signature)
      commandKeysRef.current.set(operation, {
        signature,
        key: crypto.randomUUID(),
      });
    return commandKeysRef.current.get(operation)!.key;
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
      commandKeysRef.current.delete("create");
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

  return {
    repositoryId,
    snapshotId,
    csrfToken,
    queryClient,
    targetKey,
    findingId,
    findingRunId,
    setFindingId,
    setFindingRunId,
    uuid,
    invalidFinding,
    commandKeysRef,
    listQuery,
    detailQuery,
    eventsQuery,
    createMutation,
    executeMutation,
    cancelMutation,
    resumeMutation,
    retryMutation,
    feedbackMutation,
    detail,
    summary,
    actionAvailable,
    formatMicros,
    getVerdictStyle,
    getClaimBadgeStyle,
    commandKey,
    setSelectedInvId,
    query,
    setQuery,
    findingOverride,
    setFindingOverride,
    mode,
    setMode,
    maxSteps,
    setMaxSteps,
    budgetMicros,
    setBudgetMicros,
    selectedInvId,
    setSelection,
    feedbackNotes,
    setFeedbackNotes,
    feedbackType,
    setFeedbackType,
    saveToMemory,
    setSaveToMemory,
    cancelIntent,
    setCancelIntent,
    liveEvents,
    setLiveEvents,
    eventCursor,
    setEventCursor,
    historyCursor,
    setHistoryCursor,
  };
}

export type InvestigationsController = ReturnType<
  typeof useInvestigationsController
>;

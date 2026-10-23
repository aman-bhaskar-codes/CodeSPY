import type {
  SessionV1,
  RepositoryPageV1,
  SubmissionRecordV1,
  JobRecordV2,
  JobRecordV3,
  StructuralSummaryV1,
  StructuralSectionPageV1,
  InventoryPageV1,
  AnalysisAcceptedV1,
  ArchitectureReportV1,
} from "./generated/types.gen";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}
export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: "same-origin",
    redirect: "error",
  });
  if (!response.ok) {
    const value = (await response.json().catch(() => ({}))) as {
      code?: string;
    };
    throw new ApiError(response.status, value.code ?? "request_failed");
  }
  return (await response.json()) as T;
}
export const api = {
  session: (signal?: AbortSignal) => request<SessionV1>("/session", { signal }),
  repositories: (signal?: AbortSignal, cursor: string | null = null) =>
    request<RepositoryPageV1>(
      `/repositories${cursor ? `?after=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  job: (id: string, signal?: AbortSignal) =>
    request<JobRecordV2 | JobRecordV3>(`/jobs/${id}`, { signal }),
  submission: (id: string, signal?: AbortSignal) =>
    request<SubmissionRecordV1>(`/submissions/${id}`, { signal }),
  inventory: (id: string, cursor: string | null, signal?: AbortSignal) =>
    request<InventoryPageV1>(
      `/snapshots/${id}/inventory?limit=50${cursor ? `&after=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  createAnalysis: (
    repoId: string,
    snapshotId: string,
    csrf: string,
    key?: string,
    includePrefixes?: string[],
  ) =>
    request<AnalysisAcceptedV1>(`/repositories/${repoId}/analyses`, {
      method: "POST",
      headers: mutationHeaders(csrf, key),
      body: JSON.stringify({
        snapshot_id: snapshotId,
        include_prefixes: includePrefixes ?? [],
        profile: "structural-small-v1",
      }),
    }),
  analysisReport: (repoId: string, runId: string, signal?: AbortSignal) =>
    request<StructuralSummaryV1>(
      `/repositories/${repoId}/analyses/${runId}/report`,
      { signal },
    ).then((value) => hydrateReport(repoId, value, signal)),
  snapshotStructuralReport: (
    repoId: string,
    snapshotId: string,
    signal?: AbortSignal,
  ) =>
    request<StructuralSummaryV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/structural-report`,
      { signal },
    ).then((value) => hydrateReport(repoId, value, signal)),
  createScan: (
    repoId: string,
    snapshotId: string,
    csrf: string,
    key?: string,
    config?: unknown,
  ) =>
    request<{
      spec_id: string;
      job_id: string;
      snapshot_id: string;
      status_url: string;
    }>(`/repositories/${repoId}/snapshots/${snapshotId}/scans`, {
      method: "POST",
      headers: mutationHeaders(csrf, key),
      body: JSON.stringify({ config: config ?? null }),
    }),
  scanSummary: (
    repoId: string,
    snapshotId: string,
    runId: string,
    signal?: AbortSignal,
  ) =>
    request<import("../features/findings/types").ScanSummaryResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/scans/${runId}/summary`,
      { signal },
    ),
  findingsList: (
    repoId: string,
    snapshotId: string,
    runId: string,
    params: {
      category?: string;
      severity?: string;
      disposition?: string;
      triage_state?: string;
      cursor?: string;
      limit?: number;
    } = {},
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams();
    if (params.category && params.category !== "all")
      q.set("category", params.category);
    if (params.severity && params.severity !== "all")
      q.set("severity", params.severity);
    if (params.disposition && params.disposition !== "all")
      q.set("disposition", params.disposition);
    if (params.triage_state && params.triage_state !== "all")
      q.set("triage_state", params.triage_state);
    if (params.cursor) q.set("cursor", params.cursor);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString() ? `?${q.toString()}` : "";
    return request<import("../features/findings/types").FindingListResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/scans/${runId}/findings${qs}`,
      { signal },
    );
  },
  findingDetail: (
    repoId: string,
    snapshotId: string,
    findingId: string,
    signal?: AbortSignal,
  ) =>
    request<import("../features/findings/types").FindingDetailResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/findings/${findingId}`,
      { signal },
    ),
  triageFinding: (
    repoId: string,
    snapshotId: string,
    findingId: string,
    transition: import("../features/findings/types").TriageTransitionV1,
    csrf: string,
    key?: string,
  ) =>
    request<import("../features/findings/types").TriageRecordV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/findings/${findingId}/triage`,
      {
        method: "POST",
        headers: mutationHeaders(csrf, key),
        body: JSON.stringify(transition),
      },
    ),
  triageHistory: (
    repoId: string,
    snapshotId: string,
    findingId: string,
    signal?: AbortSignal,
    cursor?: string,
    limit: number = 50,
  ) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    return request<import("../features/findings/types").TriageHistoryV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/findings/${findingId}/triage/history?${query}`,
      { signal },
    );
  },
  featureTrace: (
    repoId: string,
    snapshotId: string,
    findingId: string,
    budget?: import("../features/findings/types").TraceBudgetV1,
    csrf?: string,
    key?: string,
  ) =>
    request<import("../features/findings/types").FeatureTraceV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/findings/${findingId}/trace`,
      {
        method: "POST",
        headers: mutationHeaders(csrf ?? "", key),
        body: JSON.stringify(budget ?? {}),
      },
    ),
  searchExact: (
    repoId: string,
    snapshotId: string,
    query: string,
    limit: number = 20,
    csrf?: string,
    key?: string,
    signal?: AbortSignal,
  ) =>
    request<import("../features/retrieval/types").SearchResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/search/exact`,
      {
        method: "POST",
        headers: mutationHeaders(csrf ?? "", key),
        body: JSON.stringify({ query, limit }),
        signal,
      },
    ),
  searchLexical: (
    repoId: string,
    snapshotId: string,
    query: string,
    limit: number = 20,
    csrf?: string,
    key?: string,
    signal?: AbortSignal,
  ) =>
    request<import("../features/retrieval/types").SearchResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/search/lexical`,
      {
        method: "POST",
        headers: mutationHeaders(csrf ?? "", key),
        body: JSON.stringify({ query, limit }),
        signal,
      },
    ),
  searchDense: (
    repoId: string,
    snapshotId: string,
    query: string,
    indexId?: string,
    limit: number = 20,
    csrf?: string,
    key?: string,
    signal?: AbortSignal,
  ) =>
    request<import("../features/retrieval/types").DenseSearchResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/search/dense`,
      {
        method: "POST",
        headers: mutationHeaders(csrf ?? "", key),
        body: JSON.stringify({ query, index_id: indexId, limit }),
        signal,
      },
    ),
  searchHybrid: (
    repoId: string,
    snapshotId: string,
    query: string,
    limit: number = 20,
    denseIndexId?: string,
    csrf?: string,
    key?: string,
    signal?: AbortSignal,
  ) =>
    request<import("../features/retrieval/types").HybridSearchResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/search/hybrid`,
      {
        method: "POST",
        headers: mutationHeaders(csrf ?? "", key),
        body: JSON.stringify({
          query,
          limit,
          dense_index_id: denseIndexId,
        }),
        signal,
      },
    ),
  assembleContext: (
    repoId: string,
    snapshotId: string,
    body: import("../features/retrieval/types").ContextAssemblyRequestV1,
    csrf?: string,
    key?: string,
    signal?: AbortSignal,
  ) =>
    request<import("../features/retrieval/types").ContextPacketV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/context/assemble`,
      {
        method: "POST",
        headers: mutationHeaders(csrf ?? "", key),
        body: JSON.stringify(body),
        signal,
      },
    ),
  recordMemory: (
    repoId: string,
    snapshotId: string,
    body: import("../features/retrieval/types").MemoryCreateRequestV1,
    csrf: string,
    key?: string,
  ) =>
    request<import("../features/retrieval/types").MemoryEntryV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/memories`,
      {
        method: "POST",
        headers: mutationHeaders(csrf, key),
        body: JSON.stringify(body),
      },
    ),
  queryMemories: (
    repoId: string,
    snapshotId: string,
    params: { query?: string; memory_kind?: string; topic?: string } = {},
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams();
    if (params.query) q.set("query", params.query);
    if (params.memory_kind) q.set("memory_kind", params.memory_kind);
    if (params.topic) q.set("topic", params.topic);
    const qs = q.toString() ? `?${q.toString()}` : "";
    return request<import("../features/retrieval/types").MemoryQueryResponseV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/memories${qs}`,
      { signal },
    );
  },
  sourceView: (
    repoId: string,
    snapshotId: string,
    fileId: string,
    params: {
      start_line?: number;
      end_line?: number;
      representation?: string;
    } = {},
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams();
    if (params.start_line) q.set("start_line", String(params.start_line));
    if (params.end_line) q.set("end_line", String(params.end_line));
    if (params.representation) q.set("representation", params.representation);
    const qs = q.toString() ? `?${q.toString()}` : "";
    return request<import("../features/retrieval/types").SourceViewResultV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/files/${fileId}/source-view${qs}`,
      { signal },
    );
  },
  createInvestigation: (
    repoId: string,
    req: import("../features/investigations/types").InvestigationCreateRequestV1,
    csrf: string,
    key?: string,
  ) =>
    request<import("../features/investigations/types").InvestigationSummaryV1>(
      `/repositories/${repoId}/investigations`,
      {
        method: "POST",
        headers: mutationHeaders(csrf, key),
        body: JSON.stringify(req),
      },
    ),
  listInvestigations: (
    repoId: string,
    findingId?: string,
    signal?: AbortSignal,
  ) => {
    const qs = findingId ? `?finding_id=${encodeURIComponent(findingId)}` : "";
    return request<
      import("../features/investigations/types").InvestigationListResponseV1
    >(`/repositories/${repoId}/investigations${qs}`, { signal });
  },
  getInvestigation: (
    repoId: string,
    investigationId: string,
    signal?: AbortSignal,
  ) =>
    request<
      import("../features/investigations/types").InvestigationDetailResponseV1
    >(`/repositories/${repoId}/investigations/${investigationId}`, { signal }),
  executeInvestigation: (
    repoId: string,
    investigationId: string,
    csrf: string,
    key?: string,
  ) =>
    request<
      import("../features/investigations/types").InvestigationDetailResponseV1
    >(`/repositories/${repoId}/investigations/${investigationId}/execute`, {
      method: "POST",
      headers: mutationHeaders(csrf, key),
    }),
  listInvestigationEvents: (
    repoId: string,
    investigationId: string,
    afterStep?: number,
    signal?: AbortSignal,
  ) => {
    const qs = afterStep !== undefined ? `?after_step=${afterStep}` : "";
    return request<
      import("../features/investigations/types").InvestigationEventV1[]
    >(`/repositories/${repoId}/investigations/${investigationId}/events${qs}`, {
      signal,
    });
  },
  submitInvestigationFeedback: (
    repoId: string,
    investigationId: string,
    feedback: import("../features/investigations/types").InvestigationFeedbackRequestV1,
    csrf: string,
    key?: string,
  ) =>
    request<{
      status: string;
      feedback_type: string;
      memory_entry_id?: string | null;
    }>(`/repositories/${repoId}/investigations/${investigationId}/feedback`, {
      method: "POST",
      headers: mutationHeaders(csrf, key),
      body: JSON.stringify(feedback),
    }),
};
export function mutationHeaders(
  csrf: string,
  key: string = crypto.randomUUID(),
): Record<string, string> {
  return {
    "X-CSRF-Token": csrf,
    "Idempotency-Key": key,
    "Content-Type": "application/json",
  };
}

async function hydrateReport(
  repoId: string,
  value: StructuralSummaryV1,
  signal?: AbortSignal,
): Promise<ArchitectureReportV1> {
  const report = { ...value } as ArchitectureReportV1;
  // Every response and collection has a server-enforced cap. Fetch only a user-selected report.
  for (const section of [
    "symbols",
    "resolutions",
    "routes",
    "requests",
    "links",
    "diagnostics",
    "components",
    "symbol_map",
    "evidence_map",
    "excerpts",
  ] as const) {
    const items: unknown[] = [];
    let after: string | null = null;
    do {
      const page: StructuralSectionPageV1 = await request(
        `/repositories/${repoId}/analyses/${value.run_ref.run_id}/report/${section}?limit=100${after ? `&after=${encodeURIComponent(after)}` : ""}`,
        { signal },
      );
      items.push(...page.items);
      after = page.next_cursor ?? null;
      if (items.length > 10000) throw new Error("Report capacity exceeded");
    } while (after);
    Object.assign(report, { [section]: items });
  }
  return report;
}

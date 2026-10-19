import type {
  SessionV1,
  RepositoryPageV1,
  SubmissionRecordV1,
  JobRecordV2,
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
    request<JobRecordV2>(`/jobs/${id}`, { signal }),
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
    request<ArchitectureReportV1>(
      `/repositories/${repoId}/analyses/${runId}/report`,
      { signal },
    ),
  snapshotStructuralReport: (
    repoId: string,
    snapshotId: string,
    signal?: AbortSignal,
  ) =>
    request<ArchitectureReportV1>(
      `/repositories/${repoId}/snapshots/${snapshotId}/structural-report`,
      { signal },
    ),
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

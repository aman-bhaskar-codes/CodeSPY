import { useEffect } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, mutationHeaders, request } from "../api/client";
import type { RetryResponseV2 } from "../api/generated/types.gen";
import type { WorkspaceDeps } from "./controllerDeps";
import { terminal } from "./problems";

/** Structural analysis of the selected snapshot: report, job, and its commands. */
export function useAnalysis(
  { client, session, selection, retryKeys }: WorkspaceDeps,
  snapshot: string | null | undefined,
) {
  const { repository, analysisJobId, setAnalysisJobId, responseIsCurrent } =
    selection;
  const structuralReport = useQuery({
    queryKey: ["structuralReport", repository, snapshot],
    queryFn: ({ signal }) =>
      api.snapshotStructuralReport(repository, snapshot!, signal),
    enabled: !!snapshot && !!repository && !!session.data,
    retry: false,
  });
  const analysisJob = useQuery({
    queryKey: ["job", analysisJobId],
    queryFn: ({ signal }) => api.job(analysisJobId!, signal),
    enabled: !!analysisJobId && !!session.data,
    refetchInterval: (query) =>
      query.state.data && terminal.has(query.state.data.state) ? false : 1500,
  });
  useEffect(() => {
    if (
      analysisJob.data?.state === "succeeded" ||
      analysisJob.data?.state === "partial"
    ) {
      void client.invalidateQueries({
        queryKey: ["structuralReport", repository, snapshot],
      });
    }
  }, [analysisJob.data?.state, client, repository, snapshot]);
  const analyze = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!repository || !snapshot || !session.data)
        throw new Error("missing input");
      const res = await api.createAnalysis(
        repository,
        snapshot,
        session.data.csrf_token,
      );
      if (isCurrent()) setAnalysisJobId(res.job_id);
    },
  });
  const cancelAnalysis = useMutation({
    mutationFn: async () => {
      if (!analysisJobId || !session.data) throw new Error("missing analysis");
      await request(`/jobs/${analysisJobId}/cancel`, {
        method: "POST",
        headers: mutationHeaders(session.data.csrf_token),
      });
      await client.invalidateQueries({ queryKey: ["job", analysisJobId] });
    },
  });
  const retryAnalysis = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!analysisJobId || !session.data) throw new Error("missing analysis");
      let key = retryKeys.current.get(analysisJobId);
      if (!key) {
        key = crypto.randomUUID();
        retryKeys.current.set(analysisJobId, key);
      }
      const result = await request<RetryResponseV2>(
        `/jobs/${analysisJobId}/retry`,
        {
          method: "POST",
          headers: mutationHeaders(session.data.csrf_token, key),
        },
      );
      if (isCurrent()) setAnalysisJobId(result.retry_job_id);
    },
  });
  return {
    structuralReport,
    analysisJob,
    analyze,
    cancelAnalysis,
    retryAnalysis,
  };
}

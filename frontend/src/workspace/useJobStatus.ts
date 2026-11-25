import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { terminal } from "./problems";
import type { SessionQuery } from "./useSessionData";

/** The submission and the inventory job being followed, and the snapshot they resolve to. */
export function useJobStatus(
  session: SessionQuery,
  submissionId: string | null,
  jobId: string | null,
  restoredSnapshot: string | null,
) {
  const submission = useQuery({
    queryKey: ["submission", submissionId],
    queryFn: ({ signal }) => api.submission(submissionId!, signal),
    enabled: !!submissionId && !!session.data,
    refetchInterval: (query) =>
      query.state.data &&
      ["admitted", "rejected", "failed", "cancelled", "expired"].includes(
        query.state.data.state,
      )
        ? false
        : 1500,
  });
  const anchorJob = jobId ?? submission.data?.job_id;
  const parentJob = useQuery({
    queryKey: ["job", anchorJob],
    queryFn: ({ signal }) => api.job(anchorJob!, signal),
    enabled: !!anchorJob && !!session.data,
    refetchInterval: (query) =>
      query.state.data && terminal.has(query.state.data.state) ? false : 1500,
  });
  const selectedJob = parentJob.data?.inventory_job_id ?? anchorJob;
  const job = useQuery({
    queryKey: ["job", selectedJob],
    queryFn: ({ signal }) => api.job(selectedJob!, signal),
    enabled: !!selectedJob && !!session.data,
    refetchInterval: (query) =>
      query.state.data && terminal.has(query.state.data.state) ? false : 2000,
  });
  const snapshot =
    submission.data?.snapshot_id ?? job.data?.snapshot_id ?? restoredSnapshot;
  return { submission, job, selectedJob, snapshot };
}

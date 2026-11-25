import { useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { mutationHeaders, request } from "../api/client";
import type { RetryResponseV1 } from "../api/generated/types.gen";
import type { WorkspaceDeps } from "./controllerDeps";

/** Cancel or retry the inventory job being followed. */
export function useJobControl(
  { client, session, selection, retryKeys }: WorkspaceDeps,
  selectedJob: string | null | undefined,
) {
  const {
    setRestoredSnapshot,
    setJobId,
    setSubmissionId,
    setPages,
    setConfirmCancel,
    responseIsCurrent,
  } = selection;
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const action = useMutation({
    mutationFn: async (kind: "cancel" | "retry") => {
      const isCurrent = responseIsCurrent();
      if (!selectedJob || !session.data) throw new Error("missing job");
      const storageKey = `codespy:retry:${selectedJob}`;
      let commandKey = retryKeys.current.get(selectedJob);
      if (kind === "retry" && !commandKey) {
        try {
          commandKey = window.sessionStorage.getItem(storageKey) ?? undefined;
        } catch {
          /* private-storage fallback */
        }
        commandKey ??= crypto.randomUUID();
        retryKeys.current.set(selectedJob, commandKey);
        try {
          window.sessionStorage.setItem(storageKey, commandKey);
        } catch {
          /* retained in memory */
        }
      }
      const result = await request<RetryResponseV1 | object>(
        `/jobs/${selectedJob}/${kind}`,
        {
          method: "POST",
          headers: mutationHeaders(session.data.csrf_token, commandKey),
        },
      );
      if (isCurrent() && kind === "retry" && "retry_job_id" in result) {
        const retried = result as RetryResponseV1;
        setRestoredSnapshot(null);
        setJobId(retried.retry_job_id);
        setSubmissionId(retried.submission_id);
        setPages([null]);
      }
      setConfirmCancel(false);
      cancelButtonRef.current?.focus();
      await client.invalidateQueries({ queryKey: ["job"] });
      await client.invalidateQueries({ queryKey: ["submission"] });
    },
  });
  return { action, cancelButtonRef };
}

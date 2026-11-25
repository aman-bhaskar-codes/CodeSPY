import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { WorkspaceDeps } from "./controllerDeps";
import { terminal } from "./problems";

/** Security scan of the selected snapshot: scope, job, and its commands. */
export function useScan(
  { client, session, selection, retryKeys }: WorkspaceDeps,
  snapshot: string | null | undefined,
) {
  const { repository, scanJobId, setScanJobId, responseIsCurrent } = selection;
  const scanConfiguration = useQuery({
    queryKey: ["scan-configuration", repository, snapshot],
    queryFn: ({ signal }) =>
      api.scanConfiguration(repository, snapshot!, signal),
    enabled: !!repository && !!snapshot && !!session.data,
    retry: false,
  });
  const scanJob = useQuery({
    queryKey: ["scan-job", repository, snapshot, scanJobId],
    queryFn: ({ signal }) =>
      api.scanJob(repository, snapshot!, scanJobId!, signal),
    enabled: !!repository && !!snapshot && !!scanJobId && !!session.data,
    refetchInterval: (query) =>
      terminal.has(query.state.data?.state ?? "") ? false : 1500,
  });
  const startScan = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (
        !repository ||
        !snapshot ||
        !session.data ||
        !scanConfiguration.data?.config
      )
        throw new Error("scan_configuration_unavailable");
      const key = `scan:${repository}:${snapshot}`;
      if (!retryKeys.current.has(key))
        retryKeys.current.set(key, crypto.randomUUID());
      const result = await api.createScan(
        repository,
        snapshot,
        session.data.csrf_token,
        retryKeys.current.get(key),
        scanConfiguration.data.config,
      );
      if (isCurrent()) setScanJobId(result.job_id);
      retryKeys.current.delete(key);
    },
  });
  const cancelScan = useMutation({
    mutationFn: async () => {
      if (!scanJobId || !snapshot || !session.data)
        throw new Error("missing scan");
      await api.cancelScan(
        repository,
        snapshot,
        scanJobId,
        session.data.csrf_token,
        `cancel-${scanJobId}`,
      );
      await client.invalidateQueries({
        queryKey: ["scan-job", repository, snapshot, scanJobId],
      });
    },
  });
  const retryScan = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!scanJobId || !snapshot || !session.data)
        throw new Error("missing scan");
      if (!retryKeys.current.has(scanJobId))
        retryKeys.current.set(scanJobId, crypto.randomUUID());
      const result = await api.retryScan(
        repository,
        snapshot,
        scanJobId,
        session.data.csrf_token,
        retryKeys.current.get(scanJobId)!,
      );
      if (isCurrent()) setScanJobId(result.retry_job_id);
    },
  });
  return { scanConfiguration, scanJob, startScan, cancelScan, retryScan };
}

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { SessionQuery } from "./useSessionData";

/** One page of the snapshot's file inventory. */
export function useInventory(
  session: SessionQuery,
  repository: string,
  snapshot: string | null | undefined,
  pages: (string | null)[],
) {
  return useQuery({
    queryKey: ["inventory", repository, snapshot, pages.at(-1)],
    queryFn: ({ signal }) =>
      api.inventory(
        snapshot!,
        pages.at(-1) ?? null,
        signal,
        repository || undefined,
      ),
    enabled: !!snapshot && !!session.data,
  });
}

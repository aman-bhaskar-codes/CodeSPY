import type { QueryClient } from "@tanstack/react-query";
import type { SessionQuery } from "./useSessionData";
import type { useWorkspaceSelection } from "./useWorkspaceSelection";

/** What every workspace hook needs: the cache, the session, the selection, and command keys. */
export interface WorkspaceDeps {
  client: QueryClient;
  session: SessionQuery;
  selection: ReturnType<typeof useWorkspaceSelection>;
  /** Idempotency keys per intent, so a retried command reuses its key after a lost response. */
  retryKeys: { current: Map<string, string> };
}

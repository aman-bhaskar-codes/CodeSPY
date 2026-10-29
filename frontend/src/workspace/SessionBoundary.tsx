import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { WorkspaceShell } from "./WorkspaceShell";
import { Panel } from "../ui/Panel";
import { Button } from "../ui/Button";
import { ApiError, invalidateAccessResponses } from "../api/client";

export function SessionBoundary({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [blocked, setBlocked] = useState(false);
  const lastPrincipal = useRef<string | null>(null);
  useEffect(() => {
    const reset = () => {
      invalidateAccessResponses();
      setBlocked(true);
      void client.cancelQueries();
      client.clear();
    };
    window.addEventListener("codespy:access-required", reset);
    const unsubscribe = client.getQueryCache().subscribe((event) => {
      if (event.type !== "updated") return;
      const error = event.query.state.error;
      if (
        event.action.type === "error" &&
        error instanceof ApiError &&
        [401, 403, 404].includes(error.status)
      )
        event.query.setState({ data: undefined });
      if (
        event.query.queryKey[0] === "session" &&
        event.action.type === "success"
      ) {
        const data = event.query.state.data;
        const principal =
          data && typeof data === "object" && "principal_id" in data
            ? data.principal_id
            : null;
        if (typeof principal !== "string") return;
        const previous = lastPrincipal.current;
        lastPrincipal.current = principal;
        if (previous && previous !== principal) reset();
      }
    });
    return () => {
      window.removeEventListener("codespy:access-required", reset);
      unsubscribe();
    };
  }, [client]);
  if (!blocked) return children;
  return (
    <WorkspaceShell sessionState="unavailable" destinations={[]}>
      <Panel id="access-changed" title="Workspace access changed" role="alert">
        <p>
          Cached repository data has been cleared. Reload access or sign in
          before continuing.
        </p>
        <div className="session-actions">
          <Button onClick={() => setBlocked(false)}>Reload access</Button>
          <a href="/api/v1/auth/github/start">Sign in with GitHub</a>
        </div>
      </Panel>
    </WorkspaceShell>
  );
}

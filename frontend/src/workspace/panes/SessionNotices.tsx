import { Panel } from "../../ui/Panel";
import { Button } from "../../ui/Button";
import { useWorkspace } from "../workspaceContext";

/** Sign-out failure, the session check, and the unavailable-sign-in panel. */
export function SessionNotices() {
  const { session, logout } = useWorkspace();
  return (
    <>
      {logout.isError && (
        <p role="alert">Sign-out could not be confirmed. Retry signing out.</p>
      )}
      {session.isPending && <p role="status">Checking your session…</p>}
      {session.isError && (
        <Panel
          id="session-unavailable"
          title="Sign-in is unavailable"
          role="alert"
        >
          <p>
            The local fixture or GitHub sign-in provider must be configured
            before you can submit sources.
          </p>
          <div className="session-actions">
            <Button onClick={() => void session.refetch()}>
              Retry session
            </Button>
            <a href="/api/v1/auth/github/start">Sign in with GitHub</a>
          </div>
        </Panel>
      )}
    </>
  );
}

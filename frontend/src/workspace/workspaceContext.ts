import { createContext, useContext } from "react";
import type { WorkspaceController } from "./useWorkspaceController";

export const WorkspaceContext = createContext<WorkspaceController | null>(null);

/** The shared workspace state. Only valid under the workspace provider. */
export function useWorkspace(): WorkspaceController {
  const value = useContext(WorkspaceContext);
  if (value === null)
    throw new Error("useWorkspace needs the workspace provider");
  return value;
}

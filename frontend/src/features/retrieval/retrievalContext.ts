import { createContext, useContext } from "react";
import type { RetrievalController } from "./useRetrievalController";

export const RetrievalContext = createContext<RetrievalController | null>(null);

/** The retrieval workbench state. Only valid under the workbench provider. */
export function useRetrieval(): RetrievalController {
  const value = useContext(RetrievalContext);
  if (value === null)
    throw new Error("useRetrieval needs the workbench provider");
  return value;
}

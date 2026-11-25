import { createContext, useContext } from "react";
import type { InvestigationsController } from "./useInvestigationsController";

export const InvestigationsContext =
  createContext<InvestigationsController | null>(null);

/** The workbench state. Only valid under the workbench provider. */
export function useInvestigations(): InvestigationsController {
  const value = useContext(InvestigationsContext);
  if (value === null)
    throw new Error("useInvestigations needs the workbench provider");
  return value;
}

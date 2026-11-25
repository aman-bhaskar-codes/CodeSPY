import { ModelAvailabilityPanel } from "./ModelAvailabilityPanel";
import type { Props } from "./investigationTypes";
import { InvestigationsContext } from "./investigationsContext";
import { ActiveInvestigation } from "./parts/ActiveInvestigation";
import { CancelConfirmation } from "./parts/CancelConfirmation";
import { InvestigationBanner } from "./parts/InvestigationBanner";
import { InvestigationHistory } from "./parts/InvestigationHistory";
import { InvestigationNotices } from "./parts/InvestigationNotices";
import { NewInvestigationForm } from "./parts/NewInvestigationForm";
import { useInvestigationsController } from "./useInvestigationsController";

/**
 * Launch, follow and review investigations. State and commands live in the controller hook;
 * each region of the screen is a part that reads them from context.
 */
export function InvestigationsWorkbench(props: Props) {
  const controller = useInvestigationsController(props);
  return (
    <InvestigationsContext value={controller}>
      <div
        id="investigations-workbench"
        style={{
          padding: "24px",
          display: "flex",
          flexDirection: "column",
          gap: "24px",
          background: "var(--cs-panel)",
          color: "var(--cs-text)",
          borderRadius: "12px",
          fontFamily: "var(--cs-font)",
        }}
      >
        <CancelConfirmation />
        <ModelAvailabilityPanel repositoryId={props.repositoryId} />
        <InvestigationNotices />
        <InvestigationBanner />
        <div
          className="investigation-columns"
          style={{
            display: "grid",
            gap: "24px",
          }}
        >
          <div
            style={{ display: "flex", flexDirection: "column", gap: "20px" }}
          >
            <NewInvestigationForm />
            <InvestigationHistory />
          </div>
          <ActiveInvestigation />
        </div>
      </div>
    </InvestigationsContext>
  );
}

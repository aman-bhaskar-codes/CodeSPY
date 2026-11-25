import { useInvestigations } from "../investigationsContext";
import { BudgetMetrics } from "./BudgetMetrics";
import { ClaimsSection } from "./ClaimsSection";
import { DetailHeader } from "./DetailHeader";
import { EventTimeline } from "./EventTimeline";
import { FailureBanner } from "./FailureBanner";
import { FeedbackSection } from "./FeedbackSection";
import { HumanDecisionBanner } from "./HumanDecisionBanner";
import { VerdictBanner } from "./VerdictBanner";

/** The selected investigation, or the prompt to select or launch one. */
export function ActiveInvestigation() {
  const { summary } = useInvestigations();
  return (
    <div
      style={{
        padding: "20px",
        background: "var(--cs-raised)",
        borderRadius: "8px",
        border: "1px solid var(--cs-border)",
        display: "flex",
        flexDirection: "column",
        gap: "20px",
      }}
    >
      {summary ? (
        <>
          <DetailHeader />
          <HumanDecisionBanner />
          <FailureBanner />
          <BudgetMetrics />
          <VerdictBanner />
          <ClaimsSection />
          <EventTimeline />
          <FeedbackSection />
        </>
      ) : (
        <div
          style={{
            padding: "48px 0",
            textAlign: "center",
            color: "var(--cs-muted)",
          }}
        >
          Select an investigation from the left or launch a new hypothesis to
          view evidence details.
        </div>
      )}
    </div>
  );
}

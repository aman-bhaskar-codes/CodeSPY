import { useInvestigations } from "../investigationsContext";

/** Shown while an investigation waits for a person. */
export function HumanDecisionBanner() {
  const { summary: maybeSummary } = useInvestigations();
  if (!maybeSummary) return null;
  const summary = maybeSummary;
  return (
    <>
      {summary.status === "awaiting_human_decision" && (
        <div
          id="human-decision-banner"
          style={{
            padding: "14px",
            borderRadius: "8px",
            background: "var(--cs-warning-bg)",
            border: "1px solid var(--cs-warning)",
            color: "var(--cs-warning)",
            fontSize: "13px",
          }}
        >
          <div style={{ fontWeight: 700, marginBottom: "4px" }}>
            Human Decision Required
          </div>
          <div>
            This investigation has reached a decision checkpoint that requires
            operator authorization or resolution before proceeding. Click Resume
            to continue execution.
          </div>
        </div>
      )}
    </>
  );
}

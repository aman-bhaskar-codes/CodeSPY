import { type ExtendedSummary } from "../investigationTypes";
import { useInvestigations } from "../investigationsContext";

/** Shown when an investigation failed. */
export function FailureBanner() {
  const { summary: maybeSummary } = useInvestigations();
  if (!maybeSummary) return null;
  const summary = maybeSummary;
  return (
    <>
      {(summary as unknown as ExtendedSummary).failure_reason && (
        <div
          id="failure-reason-banner"
          style={{
            padding: "12px",
            borderRadius: "8px",
            background: "var(--cs-danger-bg)",
            border: "1px solid var(--cs-danger)",
            color: "var(--cs-danger)",
            fontSize: "12px",
          }}
        >
          <strong>Failure Reason: </strong>{" "}
          {(summary as unknown as ExtendedSummary).failure_reason}
        </div>
      )}
    </>
  );
}

import { useInvestigations } from "../investigationsContext";

/** The verdict and its summary. */
export function VerdictBanner() {
  const { summary: maybeSummary, getVerdictStyle } = useInvestigations();
  if (!maybeSummary) return null;
  const summary = maybeSummary;
  return (
    <>
      {summary.verdict && (
        <div
          id="verdict-banner"
          style={{
            padding: "16px",
            borderRadius: "8px",
            ...getVerdictStyle(summary.verdict),
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "8px",
            }}
          >
            <strong style={{ fontSize: "15px" }}>
              VERDICT: {summary.verdict.toUpperCase().replace("_", " ")}
            </strong>
            {summary.verdict_confidence != null && (
              <span
                id="verdict-confidence-badge"
                style={{ fontSize: "13px", fontWeight: 600 }}
              >
                Reported confidence:{" "}
                {(summary.verdict_confidence * 100).toFixed(0)}%
              </span>
            )}
          </div>
          <p style={{ margin: 0, fontSize: "13px", lineHeight: "1.5" }}>
            {summary.summary}
          </p>
        </div>
      )}
    </>
  );
}

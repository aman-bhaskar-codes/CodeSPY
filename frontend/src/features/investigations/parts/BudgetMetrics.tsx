import { type ExtendedSummary } from "../investigationTypes";
import { useInvestigations } from "../investigationsContext";

/** Budget and progress figures. */
export function BudgetMetrics() {
  const { summary: maybeSummary, formatMicros } = useInvestigations();
  if (!maybeSummary) return null;
  const summary = maybeSummary;
  return (
    <>
      <div
        id="investigation-metrics-grid"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
          gap: "12px",
          padding: "12px",
          background: "var(--cs-panel)",
          borderRadius: "8px",
          border: "1px solid var(--cs-border)",
        }}
      >
        <div>
          <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
            Step Progress
          </div>
          <div
            style={{
              fontSize: "14px",
              fontWeight: 700,
              color: "var(--cs-text)",
            }}
          >
            {summary.current_step ?? 0} / {summary.max_steps ?? 10}
          </div>
        </div>
        <div>
          <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
            Settled Spend
          </div>
          <div
            id="metric-spent-micros"
            style={{
              fontSize: "14px",
              fontWeight: 700,
              color: "var(--cs-action)",
            }}
          >
            {formatMicros(summary.spent_micros ?? 0)}
          </div>
        </div>
        <div>
          <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
            Held Budget
          </div>
          <div
            id="metric-held-micros"
            style={{
              fontSize: "14px",
              fontWeight: 700,
              color: (summary as unknown as ExtendedSummary).held_micros
                ? "var(--cs-warning)"
                : "var(--cs-muted)",
            }}
          >
            {formatMicros(
              (summary as unknown as ExtendedSummary).held_micros ?? 0,
            )}
          </div>
        </div>
        <div>
          <div style={{ fontSize: "11px", color: "var(--cs-muted)" }}>
            Budget Limit
          </div>
          <div
            style={{
              fontSize: "14px",
              fontWeight: 700,
              color: "var(--cs-action)",
            }}
          >
            {formatMicros(summary.budget_limit_micros ?? 0)}
          </div>
        </div>
      </div>
    </>
  );
}

import { useInvestigations } from "../investigationsContext";

/** Heading and the launch hint. */
export function InvestigationBanner() {
  const { snapshotId, formatMicros, mode, budgetMicros } = useInvestigations();
  return (
    <>
      <div
        className="investigation-heading"
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          borderBottom: "1px solid var(--cs-border)",
          paddingBottom: "16px",
        }}
      >
        <div>
          <h2
            id="workbench-title"
            tabIndex={-1}
            style={{
              margin: 0,
              fontSize: "20px",
              fontWeight: 700,
              color: "var(--cs-action)",
            }}
          >
            Investigation workbench
          </h2>
          <p
            style={{
              margin: "4px 0 0",
              fontSize: "13px",
              color: "var(--cs-muted)",
            }}
          >
            Investigate questions with source evidence, bounded tools and saved
            results.
          </p>
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "12px",
            alignItems: "center",
          }}
        >
          <div
            style={{
              padding: "6px 12px",
              background: "var(--cs-raised)",
              borderRadius: "6px",
              fontSize: "12px",
              border: "1px solid var(--cs-border)",
            }}
          >
            <span style={{ color: "var(--cs-muted)" }}>Mode: </span>
            <strong style={{ color: "var(--cs-action)" }}>
              {mode === "short_path" ? "Short Path" : "Two Specialist"}
            </strong>
          </div>
          {snapshotId && (
            <div
              style={{
                padding: "6px 12px",
                background: "var(--cs-raised)",
                borderRadius: "6px",
                fontSize: "12px",
                border: "1px solid var(--cs-border)",
              }}
            >
              <span style={{ color: "var(--cs-muted)" }}>Snapshot: </span>
              <strong style={{ color: "var(--cs-text)" }}>
                {snapshotId.slice(0, 8)}
              </strong>
            </div>
          )}
          <div
            style={{
              padding: "6px 12px",
              background: "var(--cs-raised)",
              borderRadius: "6px",
              fontSize: "12px",
              border: "1px solid var(--cs-border)",
            }}
          >
            <span style={{ color: "var(--cs-muted)" }}>Budget Limit: </span>
            <strong style={{ color: "var(--cs-action)" }}>
              {formatMicros(budgetMicros)}
            </strong>
          </div>
        </div>
      </div>
    </>
  );
}

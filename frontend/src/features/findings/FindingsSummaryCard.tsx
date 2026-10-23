import type { ScanSummaryResponseV1 } from "./types";

interface Props {
  summary: ScanSummaryResponseV1;
}

export function FindingsSummaryCard({ summary }: Props) {
  const severities = ["critical", "high", "medium", "low", "info"] as const;
  const dispositions = [
    "proposed",
    "supported-static",
    "contradicted",
    "unresolved",
  ] as const;

  return (
    <div
      className="findings-summary-card"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "8px",
        padding: "20px",
        marginBottom: "20px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "12px",
          marginBottom: "16px",
        }}
      >
        <div>
          <h3 style={{ margin: 0, fontSize: "18px", color: "var(--ink)" }}>
            Findings Summary
          </h3>
          <span className="muted" style={{ fontSize: "13px" }}>
            Report published:{" "}
            {summary.created_at
              ? new Date(summary.created_at).toLocaleString()
              : "Publication time unavailable"}
          </span>
        </div>
        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          <span
            style={{
              fontSize: "14px",
              fontWeight: 600,
              padding: "4px 10px",
              borderRadius: "4px",
              background: "#e8effc",
              color: "var(--action)",
            }}
          >
            {summary.total_findings} total findings
          </span>
        </div>
      </div>

      {summary.total_findings === 0 && (
        <div
          role="status"
          style={{
            padding: "12px 16px",
            background: "#f0f4f8",
            border: "1px solid var(--border)",
            borderRadius: "6px",
            fontSize: "13px",
            color: "var(--muted)",
            marginBottom: "16px",
          }}
        >
          <strong>0 findings detected across scanned rules.</strong> This does not
          indicate absence of all vulnerabilities or complete security.
        </div>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
          gap: "12px",
          marginBottom: "16px",
        }}
      >
        {severities.map((sev) => {
          const count = summary.by_severity?.[sev] ?? 0;
          const colors: Record<string, { bg: string; text: string }> = {
            critical: { bg: "#fde8e8", text: "#9c2929" },
            high: { bg: "#feeedb", text: "#a55307" },
            medium: { bg: "#fef8e7", text: "#876400" },
            low: { bg: "#eef2f7", text: "#334155" },
            info: { bg: "#eef5fc", text: "#1d4ed8" },
          };
          const color = colors[sev] ?? { bg: "#f1f5f9", text: "#475569" };
          return (
            <div
              key={sev}
              style={{
                background: color.bg,
                border: "1px solid var(--border)",
                borderRadius: "6px",
                padding: "10px 14px",
              }}
            >
              <div
                style={{
                  fontSize: "11px",
                  textTransform: "uppercase",
                  fontWeight: 700,
                  color: color.text,
                }}
              >
                {sev}
              </div>
              <div
                style={{
                  fontSize: "22px",
                  fontWeight: 800,
                  color: color.text,
                }}
              >
                {count}
              </div>
            </div>
          );
        })}
      </div>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "16px",
          paddingTop: "12px",
          borderTop: "1px solid var(--border)",
          fontSize: "13px",
          color: "var(--muted)",
        }}
      >
        <span style={{ fontWeight: 600 }}>Dispositions:</span>
        {dispositions.map((disp) => (
          <span key={disp}>
            {disp}: <strong>{summary.by_disposition?.[disp] ?? 0}</strong>
          </span>
        ))}
      </div>
    </div>
  );
}

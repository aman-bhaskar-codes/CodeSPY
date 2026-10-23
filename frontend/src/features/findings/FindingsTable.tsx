import { useState } from "react";
import type {
  FindingCategory,
  FindingDisposition,
  FindingListResponseV1,
  FindingSeverity,
  FindingSummaryV1,
  TriageState,
  TriageTransitionV1,
} from "./types";
import { TriageModal } from "./TriageModal";

interface Props {
  data: FindingListResponseV1;
  filters: {
    category: string;
    severity: string;
    disposition: string;
    triage_state: string;
  };
  onFilterChange: (filters: {
    category: string;
    severity: string;
    disposition: string;
    triage_state: string;
  }) => void;
  onSelectFinding: (findingId: string) => void;
  onTriageFinding: (findingId: string, transition: TriageTransitionV1) => Promise<void>;
  hasNextPage: boolean;
  onNextPage: () => void;
  onResetPagination: () => void;
  isLoading: boolean;
}

export function FindingsTable({
  data,
  filters,
  onFilterChange,
  onSelectFinding,
  onTriageFinding,
  hasNextPage,
  onNextPage,
  onResetPagination,
  isLoading,
}: Props) {
  const [triageFindingTarget, setTriageFindingTarget] = useState<FindingSummaryV1 | null>(
    null,
  );

  const categories: Array<FindingCategory | "all"> = [
    "all",
    "source_pattern",
    "secret_format",
    "dependency_advisory",
  ];
  const severities: Array<FindingSeverity | "all"> = [
    "all",
    "critical",
    "high",
    "medium",
    "low",
    "info",
  ];
  const dispositions: Array<FindingDisposition | "all"> = [
    "all",
    "proposed",
    "supported-static",
    "contradicted",
    "unresolved",
  ];
  const triageStates: Array<TriageState | "all"> = [
    "all",
    "unreviewed",
    "confirmed",
    "false_positive",
    "mitigated",
    "accepted_risk",
  ];

  return (
    <div
      className="findings-table-wrapper"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "8px",
        overflow: "hidden",
      }}
    >
      {/* Filter Bar */}
      <div
        style={{
          padding: "16px",
          borderBottom: "1px solid var(--border)",
          display: "flex",
          flexWrap: "wrap",
          gap: "12px",
          alignItems: "center",
          background: "#fafbfc",
        }}
      >
        <div>
          <label
            htmlFor="filter-category"
            style={{ fontSize: "11px", fontWeight: 700, textTransform: "uppercase", display: "block", color: "var(--muted)", marginBottom: "4px" }}
          >
            Category
          </label>
          <select
            id="filter-category"
            value={filters.category}
            onChange={(e) => {
              onFilterChange({ ...filters, category: e.target.value });
              onResetPagination();
            }}
            style={{ padding: "6px 10px", fontSize: "13px" }}
          >
            {categories.map((c) => (
              <option key={c} value={c}>
                {c.toUpperCase()}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="filter-severity"
            style={{ fontSize: "11px", fontWeight: 700, textTransform: "uppercase", display: "block", color: "var(--muted)", marginBottom: "4px" }}
          >
            Severity
          </label>
          <select
            id="filter-severity"
            value={filters.severity}
            onChange={(e) => {
              onFilterChange({ ...filters, severity: e.target.value });
              onResetPagination();
            }}
            style={{ padding: "6px 10px", fontSize: "13px" }}
          >
            {severities.map((s) => (
              <option key={s} value={s}>
                {s.toUpperCase()}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="filter-disposition"
            style={{ fontSize: "11px", fontWeight: 700, textTransform: "uppercase", display: "block", color: "var(--muted)", marginBottom: "4px" }}
          >
            Disposition
          </label>
          <select
            id="filter-disposition"
            value={filters.disposition}
            onChange={(e) => {
              onFilterChange({ ...filters, disposition: e.target.value });
              onResetPagination();
            }}
            style={{ padding: "6px 10px", fontSize: "13px" }}
          >
            {dispositions.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="filter-triage"
            style={{ fontSize: "11px", fontWeight: 700, textTransform: "uppercase", display: "block", color: "var(--muted)", marginBottom: "4px" }}
          >
            Triage State
          </label>
          <select
            id="filter-triage"
            value={filters.triage_state}
            onChange={(e) => {
              onFilterChange({ ...filters, triage_state: e.target.value });
              onResetPagination();
            }}
            style={{ padding: "6px 10px", fontSize: "13px" }}
          >
            {triageStates.map((t) => (
              <option key={t} value={t}>
                {t.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </div>

        <div style={{ marginLeft: "auto", fontSize: "13px", color: "var(--muted)" }}>
          Showing <strong>{data.items.length}</strong> of <strong>{data.total_count}</strong> findings
        </div>
      </div>

      {/* Table */}
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
          <thead>
            <tr style={{ background: "#f1f5f9", textAlign: "left", borderBottom: "1px solid var(--border)" }}>
              <th style={{ padding: "10px 14px", width: "90px" }}>Severity</th>
              <th style={{ padding: "10px 14px", width: "80px" }}>Category</th>
              <th style={{ padding: "10px 14px" }}>Rule ID / Title</th>
              <th style={{ padding: "10px 14px" }}>File & Line</th>
              <th style={{ padding: "10px 14px", width: "110px" }}>Disposition</th>
              <th style={{ padding: "10px 14px", width: "110px" }}>Triage</th>
              <th style={{ padding: "10px 14px", width: "110px", textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={7} style={{ textAlign: "center", padding: "32px", color: "var(--muted)" }}>
                  Loading findings...
                </td>
              </tr>
            ) : data.items.length === 0 ? (
              <tr>
                <td colSpan={7} style={{ textAlign: "center", padding: "32px", color: "var(--muted)" }}>
                  No findings match the selected filters.
                </td>
              </tr>
            ) : (
              data.items.map((item) => (
                <tr
                  key={item.id}
                  style={{
                    borderBottom: "1px solid var(--border)",
                    cursor: "pointer",
                    transition: "background 0.15s ease",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "#f8fafc")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                  onClick={() => onSelectFinding(item.id)}
                >
                  <td style={{ padding: "10px 14px" }}>
                    <span
                      style={{
                        fontSize: "11px",
                        fontWeight: 700,
                        textTransform: "uppercase",
                        padding: "2px 6px",
                        borderRadius: "4px",
                        background:
                          item.severity === "critical"
                            ? "#fde8e8"
                            : item.severity === "high"
                              ? "#feeedb"
                              : "#f1f5f9",
                        color:
                          item.severity === "critical"
                            ? "#9c2929"
                            : item.severity === "high"
                              ? "#a55307"
                              : "var(--ink)",
                      }}
                    >
                      {item.severity}
                    </span>
                  </td>
                  <td style={{ padding: "10px 14px", textTransform: "uppercase", fontSize: "11px", fontWeight: 600 }}>
                    {item.category}
                  </td>
                  <td style={{ padding: "10px 14px", fontWeight: 600 }}>
                    <code>{item.rule_id}</code>
                    <div className="muted" style={{ fontSize: "12px", fontWeight: 400, marginTop: "2px" }}>
                      {item.title.slice(0, 80)}
                      {item.title.length > 80 ? "…" : ""}
                    </div>
                  </td>
                  <td style={{ padding: "10px 14px" }}>
                    <code>
                      {item.file_path}
                      {item.line_number ? `:${item.line_number}` : ""}
                    </code>
                  </td>
                  <td style={{ padding: "10px 14px", fontSize: "12px", color: "var(--muted)" }}>
                    {item.disposition}
                  </td>
                  <td style={{ padding: "10px 14px" }}>
                    <span
                      style={{
                        fontSize: "11px",
                        fontWeight: 600,
                        padding: "2px 6px",
                        borderRadius: "4px",
                        background:
                          item.triage_state === "confirmed"
                            ? "#fde8e8"
                            : item.triage_state === "false_positive"
                              ? "#f0fdf4"
                              : "#f1f5f9",
                        color:
                          item.triage_state === "confirmed"
                            ? "#9c2929"
                            : item.triage_state === "false_positive"
                              ? "#166534"
                              : "var(--muted)",
                      }}
                    >
                      {item.triage_state.replaceAll("_", " ")}
                    </span>
                  </td>
                  <td style={{ padding: "10px 14px", textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                    <button
                      type="button"
                      onClick={() => setTriageFindingTarget(item)}
                      style={{
                        padding: "4px 8px",
                        fontSize: "12px",
                        minHeight: "28px",
                        minWidth: "60px",
                        background: "transparent",
                        border: "1px solid var(--border)",
                        borderRadius: "4px",
                        cursor: "pointer",
                      }}
                    >
                      Triage
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination Footer */}
      <div
        style={{
          padding: "12px 16px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          background: "#fafbfc",
          borderTop: "1px solid var(--border)",
        }}
      >
        <span className="muted" style={{ fontSize: "12px" }}>
          Page size: 50 · Total: {data.total_count}
        </span>
        <button
          type="button"
          onClick={onNextPage}
          disabled={!hasNextPage || isLoading}
          style={{
            padding: "6px 14px",
            fontSize: "13px",
            minHeight: "32px",
            background: hasNextPage ? "var(--action)" : "transparent",
            color: hasNextPage ? "#fff" : "var(--muted)",
            border: hasNextPage ? "none" : "1px solid var(--border)",
            borderRadius: "4px",
            cursor: hasNextPage ? "pointer" : "default",
          }}
        >
          Next Page →
        </button>
      </div>

      {/* Inline Triage Modal */}
      {triageFindingTarget && (
        <TriageModal
          finding={triageFindingTarget}
          isOpen={true}
          onClose={() => setTriageFindingTarget(null)}
          onSubmit={async (transition) => {
            await onTriageFinding(triageFindingTarget.id, transition);
            setTriageFindingTarget(null);
          }}
        />
      )}
    </div>
  );
}

import { useState } from "react";
import type { ArchitectureReportV1 } from "../../api/generated/types.gen";

interface Props {
  report: ArchitectureReportV1;
  repositoryId: string;
}

export function StructuralReportView({ report, repositoryId }: Props) {
  const [bindingFilter, setBindingFilter] = useState("all");
  const [citation, setCitation] = useState<number | null>(null);
  const cov = report.coverage;
  const resolutions = (report.resolutions ?? []).filter(
    (r) => bindingFilter === "all" || r.status === bindingFilter,
  );
  const selectedExcerpt =
    citation === null ? null : report.excerpts?.[citation];
  const routes = report.routes ?? [];
  const links = report.links ?? [];
  const nonclaims = report.nonclaims ?? [];
  const symbols = report.symbols ?? [];

  const handleExport = (format: "json" | "markdown") => {
    const url = `/api/v1/repositories/${repositoryId}/analyses/${report.run_ref.run_id}/export?format=${format}`;
    window.open(url, "_blank");
  };

  return (
    <div className="structural-report">
      <div className="section-heading">
        <h3>Architecture Summary</h3>
        <span className="muted">
          Archive digest:{" "}
          <code className="hash">
            {report.archive_sha256
              ? report.archive_sha256.slice(0, 16) + "…"
              : "N/A"}
          </code>
        </span>
      </div>

      <div
        className="metrics-grid"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: "12px",
          margin: "16px 0",
        }}
      >
        <div
          className="metric-card"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: "6px",
            padding: "12px",
          }}
        >
          <div className="muted" style={{ fontSize: "12px" }}>
            Processed Files
          </div>
          <div style={{ fontSize: "20px", fontWeight: 700 }}>
            {cov.processed_files} / {cov.requested_files}
          </div>
        </div>
        <div
          className="metric-card"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: "6px",
            padding: "12px",
          }}
        >
          <div className="muted" style={{ fontSize: "12px" }}>
            Symbols Observed
          </div>
          <div style={{ fontSize: "20px", fontWeight: 700 }}>
            {symbols.length}
          </div>
        </div>
        <div
          className="metric-card"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: "6px",
            padding: "12px",
          }}
        >
          <div className="muted" style={{ fontSize: "12px" }}>
            Resolutions
          </div>
          <div style={{ fontSize: "14px", fontWeight: 600 }}>
            {cov.resolved} resolved · {cov.ambiguous} ambiguous
          </div>
        </div>
        <div
          className="metric-card"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: "6px",
            padding: "12px",
          }}
        >
          <div className="muted" style={{ fontSize: "12px" }}>
            Routes / Links
          </div>
          <div style={{ fontSize: "20px", fontWeight: 700 }}>
            {routes.length} / {links.length}
          </div>
        </div>
      </div>

      {nonclaims.length > 0 && (
        <div
          style={{
            margin: "16px 0",
            padding: "12px",
            background: "#f8fafc",
            borderLeft: "4px solid var(--action)",
            borderRadius: "4px",
          }}
        >
          <h4 style={{ margin: "0 0 8px" }}>Deterministic Scope & Nonclaims</h4>
          <ul style={{ margin: 0, paddingLeft: "20px", fontSize: "13px" }}>
            {nonclaims.map((claim, idx) => (
              <li key={idx}>{claim}</li>
            ))}
          </ul>
        </div>
      )}

      {routes.length > 0 && (
        <div style={{ margin: "16px 0" }}>
          <h4>Observed Routes ({routes.length})</h4>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Observed routes table"
          >
            <table>
              <thead>
                <tr>
                  <th scope="col">Framework</th>
                  <th scope="col">Method</th>
                  <th scope="col">Path</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                {routes.map((route, idx) => (
                  <tr key={idx}>
                    <td>{route.framework}</td>
                    <td>
                      <strong>{route.method}</strong>
                    </td>
                    <td>
                      <code>
                        {route.template_path || route.raw_path || "<unknown>"}
                      </code>
                    </td>
                    <td>{route.state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {links.length > 0 && (
        <div style={{ margin: "16px 0" }}>
          <h4>Boundary Candidate Links ({links.length})</h4>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Boundary links table"
          >
            <table>
              <thead>
                <tr>
                  <th scope="col">State</th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {links.map((link, idx) => (
                  <tr key={idx}>
                    <td>{link.state}</td>
                    <td>{link.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <section aria-label="Bindings and source evidence">
        <h4>Static bindings</h4>
        <label>
          Binding state{" "}
          <select
            value={bindingFilter}
            onChange={(event) => setBindingFilter(event.target.value)}
          >
            <option value="all">All states</option>
            <option value="resolved">Resolved</option>
            <option value="ambiguous">Ambiguous</option>
            <option value="unresolved">Unresolved</option>
          </select>
        </label>
        <p>
          {resolutions.length} binding records. Static candidates do not prove
          runtime calls.
        </p>
        <ul>
          {resolutions.slice(0, 100).map((r) => (
            <li key={`${r.site_key}-${r.resolution_kind}-${r.namespace}`}>
              {r.resolution_kind}: {r.status} — {r.reason}
            </li>
          ))}
        </ul>
        {resolutions.length > 100 && (
          <p>
            First 100 records shown. Export contains the full bounded report.
          </p>
        )}
        <h4>Source evidence</h4>
        <ul>
          {(report.excerpts ?? []).map((excerpt, index) => (
            <li key={index}>
              <button type="button" onClick={() => setCitation(index)}>
                Inspect source at line {excerpt.location.range.start_line}
              </button>
            </li>
          ))}
        </ul>
        {selectedExcerpt && (
          <div role="region" aria-label="Source excerpt">
            <p>
              File {selectedExcerpt.location.file_id} · bytes{" "}
              {selectedExcerpt.location.range.start_byte}–
              {selectedExcerpt.location.range.end_byte} · SHA256{" "}
              {selectedExcerpt.location.sha256}
            </p>
            <pre>{selectedExcerpt.text}</pre>
          </div>
        )}
        <h4>Import components</h4>
        <ul>
          {(report.components ?? []).map((component, index) => (
            <li key={index}>
              {component.relation}: {component.state},{" "}
              {component.components?.length ?? 0} components. A cycle is a
              structural observation.
            </li>
          ))}
        </ul>
        {((cov.omitted_files ?? 0) > 0 ||
          cov.syntax !== "complete" ||
          cov.resolution !== "complete") && (
          <p role="status">
            Partial coverage: omissions or unavailable sections remain visible
            in this report.
          </p>
        )}
      </section>

      <div style={{ display: "flex", gap: "10px", marginTop: "16px" }}>
        <button type="button" onClick={() => handleExport("json")}>
          Export JSON
        </button>
        <button type="button" onClick={() => handleExport("markdown")}>
          Export Markdown
        </button>
      </div>
    </div>
  );
}

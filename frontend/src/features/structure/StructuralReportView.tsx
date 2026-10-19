import type { ArchitectureReportV1 } from "../../api/generated/types.gen";

interface Props {
  report: ArchitectureReportV1;
  repositoryId: string;
}

export function StructuralReportView({ report, repositoryId }: Props) {
  const cov = report.coverage;
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
          Archive digest: <code className="hash">{report.archive_sha256 ? report.archive_sha256.slice(0, 16) + "…" : "N/A"}</code>
        </span>
      </div>

      <div className="metrics-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "12px", margin: "16px 0" }}>
        <div className="metric-card" style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "6px", padding: "12px" }}>
          <div className="muted" style={{ fontSize: "12px" }}>Processed Files</div>
          <div style={{ fontSize: "20px", fontWeight: 700 }}>
            {cov.processed_files} / {cov.requested_files}
          </div>
        </div>
        <div className="metric-card" style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "6px", padding: "12px" }}>
          <div className="muted" style={{ fontSize: "12px" }}>Symbols Observed</div>
          <div style={{ fontSize: "20px", fontWeight: 700 }}>{symbols.length}</div>
        </div>
        <div className="metric-card" style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "6px", padding: "12px" }}>
          <div className="muted" style={{ fontSize: "12px" }}>Resolutions</div>
          <div style={{ fontSize: "14px", fontWeight: 600 }}>
            {cov.resolved} resolved · {cov.ambiguous} ambiguous
          </div>
        </div>
        <div className="metric-card" style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "6px", padding: "12px" }}>
          <div className="muted" style={{ fontSize: "12px" }}>Routes / Links</div>
          <div style={{ fontSize: "20px", fontWeight: 700 }}>
            {routes.length} / {links.length}
          </div>
        </div>
      </div>

      {nonclaims.length > 0 && (
        <div style={{ margin: "16px 0", padding: "12px", background: "#f8fafc", borderLeft: "4px solid var(--action)", borderRadius: "4px" }}>
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
          <div className="table-scroll" tabIndex={0} role="region" aria-label="Observed routes table">
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
                    <td><strong>{route.method}</strong></td>
                    <td><code>{route.template_path || route.raw_path || "<unknown>"}</code></td>
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
          <div className="table-scroll" tabIndex={0} role="region" aria-label="Boundary links table">
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

import { useState } from "react";
import type { FeatureTraceV1, TraceNodeV1 } from "./types";

interface Props {
  trace: FeatureTraceV1;
}

export function FeatureTraceGraph({ trace }: Props) {
  const [selectedNode, setSelectedNode] = useState<TraceNodeV1 | null>(null);

  if (!trace.nodes?.length) {
    return (
      <div
        style={{
          padding: "24px",
          textAlign: "center",
          color: "var(--muted)",
          fontSize: "14px",
          background: "var(--cs-raised)",
          border: "1px dashed var(--border)",
          borderRadius: "6px",
        }}
      >
        No graph traversal paths observed for this finding within budget
        constraints.
      </div>
    );
  }

  // Layout calculation for simple DAG presentation
  // Group nodes by approximate depth / type
  const rootNode =
    (trace.nodes ?? []).find((n) => n.id === trace.finding_id) ??
    (trace.nodes ?? [])[0];
  const routeNodes = (trace.nodes ?? []).filter((n) => n.kind === "route");
  const intermediateNodes = (trace.nodes ?? []).filter(
    (n) => n.id !== rootNode?.id && !routeNodes.some((r) => r.id === n.id),
  );

  return (
    <div
      className="feature-trace-graph"
      style={{
        border: "1px solid var(--border)",
        borderRadius: "8px",
        background: "var(--surface)",
        padding: "16px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "12px",
          flexWrap: "wrap",
          gap: "8px",
        }}
      >
        <div style={{ fontSize: "14px", fontWeight: 650, color: "var(--ink)" }}>
          Bounded structural trace
        </div>
        <div style={{ display: "flex", gap: "8px", fontSize: "12px" }}>
          <span className="muted">
            {(trace.nodes ?? []).length} nodes · {(trace.edges ?? []).length}{" "}
            edges
          </span>
          {trace.budget_exceeded && (
            <span
              style={{
                color: "var(--cs-warning)",
                background: "var(--cs-warning-bg)",
                padding: "2px 8px",
                borderRadius: "4px",
                fontWeight: 600,
              }}
            >
              Budget Truncated
            </span>
          )}
        </div>
      </div>

      {trace.structural_run_id && (
        <p className="muted">
          Structural report: <code>{trace.structural_run_id}</code>
        </p>
      )}
      {!!trace.omission_reasons?.length && (
        <p role="status" className="muted">
          Trace coverage:{" "}
          {trace.omission_reasons
            .map((reason) => reason.replaceAll("_", " "))
            .join(", ")}
          .
        </p>
      )}
      {routeNodes.length > 0 && (
        <div
          style={{
            padding: "8px 12px",
            background: "var(--cs-info-bg)",
            border: "1px solid var(--cs-action-soft-border)",
            borderRadius: "6px",
            fontSize: "12px",
            color: "var(--cs-action)",
            marginBottom: "12px",
          }}
        >
          <strong>Connected Route Endpoints:</strong>{" "}
          {routeNodes.map((node) => node.label).join(", ")}
        </div>
      )}

      {/* Interactive Visual Graph Canvas */}
      <div
        style={{
          background: "var(--cs-raised)",
          border: "1px solid var(--border)",
          borderRadius: "6px",
          padding: "16px",
          minHeight: "260px",
          position: "relative",
          overflowX: "auto",
        }}
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr",
            gap: "24px",
            minWidth: "600px",
          }}
        >
          {/* Column 1: Root / Finding */}
          <div>
            <div
              style={{
                fontSize: "11px",
                textTransform: "uppercase",
                color: "var(--muted)",
                fontWeight: 700,
                marginBottom: "8px",
              }}
            >
              Finding location
            </div>
            {rootNode && (
              <button type="button" className="trace-node" aria-pressed={selectedNode?.id === rootNode.id}
                onClick={() => setSelectedNode(rootNode)}
                style={{
                  padding: "10px 12px",
                  background:
                    selectedNode?.id === rootNode.id
                      ? "var(--cs-info-bg)"
                      : "var(--cs-danger-bg)",
                  border:
                    selectedNode?.id === rootNode.id
                      ? "2px solid var(--cs-action)"
                      : "1px solid var(--cs-danger)",
                  borderRadius: "6px",
                  cursor: "pointer",
                  fontSize: "12px",
                }}
              >
                <div style={{ fontWeight: 700, color: "var(--cs-danger)" }}>
                  {rootNode.label}
                </div>
                <div
                  className="muted"
                  style={{ fontSize: "11px", marginTop: "4px" }}
                >
                  {rootNode.file_path}
                  {rootNode.line ? `:${rootNode.line}` : ""}
                </div>
              </button>
            )}
          </div>

          {/* Column 2: Intermediate Call / Symbol Nodes */}
          <div>
            <div
              style={{
                fontSize: "11px",
                textTransform: "uppercase",
                color: "var(--muted)",
                fontWeight: 700,
                marginBottom: "8px",
              }}
            >
              Related symbols
            </div>
            <div style={{ display: "grid", gap: "8px" }}>
              {intermediateNodes.slice(0, 8).map((node) => (
                <button type="button" className="trace-node" aria-pressed={selectedNode?.id === node.id}
                  key={node.id}
                  onClick={() => setSelectedNode(node)}
                  style={{
                    padding: "8px 10px",
                    background:
                      selectedNode?.id === node.id
                        ? "var(--cs-info-bg)"
                        : "var(--surface)",
                    border:
                      selectedNode?.id === node.id
                        ? "2px solid var(--cs-action)"
                        : "1px solid var(--border)",
                    borderRadius: "6px",
                    cursor: "pointer",
                    fontSize: "12px",
                  }}
                >
                  <div style={{ fontWeight: 600 }}>{node.label}</div>
                  <div
                    className="muted"
                    style={{ fontSize: "11px", marginTop: "2px" }}
                  >
                    kind: {node.kind}
                  </div>
                </button>
              ))}
              {intermediateNodes.length > 8 && (
                <div className="muted" style={{ fontSize: "11px" }}>
                  + {intermediateNodes.length - 8} more nodes
                </div>
              )}
            </div>
          </div>

          {/* Column 3: Route Endpoints */}
          <div>
            <div
              style={{
                fontSize: "11px",
                textTransform: "uppercase",
                color: "var(--muted)",
                fontWeight: 700,
                marginBottom: "8px",
              }}
            >
              Supported routes
            </div>
            <div style={{ display: "grid", gap: "8px" }}>
              {routeNodes.length === 0 ? (
                <div
                  className="muted"
                  style={{ fontSize: "12px", fontStyle: "italic" }}
                >
                  No supported route relationship in this trace.
                </div>
              ) : (
                routeNodes.map((node) => (
                  <button type="button" className="trace-node" aria-pressed={selectedNode?.id === node.id}
                    key={node.id}
                    onClick={() => setSelectedNode(node)}
                    style={{
                      padding: "8px 10px",
                      background:
                        selectedNode?.id === node.id
                          ? "var(--cs-info-bg)"
                          : "var(--cs-info-bg)",
                      border:
                        selectedNode?.id === node.id
                          ? "2px solid var(--cs-action)"
                          : "1px solid var(--cs-action-soft-border)",
                      borderRadius: "6px",
                      cursor: "pointer",
                      fontSize: "12px",
                    }}
                  >
                    <div style={{ fontWeight: 700, color: "var(--cs-action)" }}>
                      {node.label}
                    </div>
                    <div
                      className="muted"
                      style={{ fontSize: "11px", marginTop: "2px" }}
                    >
                      route endpoint
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Selected Node Details Drawer */}
      {selectedNode && (
        <div
          style={{
            marginTop: "12px",
            padding: "12px",
            background: "var(--cs-raised)",
            border: "1px solid var(--border)",
            borderRadius: "6px",
            fontSize: "13px",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              marginBottom: "4px",
            }}
          >
            <strong>Node Inspector: {selectedNode.label}</strong>
            <button
              type="button"
              onClick={() => setSelectedNode(null)}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--muted)",
                cursor: "pointer",
                padding: "0 4px",
              }}
            >
              ✕
            </button>
          </div>
          <div style={{ display: "grid", gap: "4px", fontSize: "12px" }}>
            <div>
              <span className="muted">Kind:</span>{" "}
              <code>{selectedNode.kind}</code>
            </div>
            {selectedNode.file_path && (
              <div>
                <span className="muted">File:</span>{" "}
                <code>{selectedNode.file_path}</code>
                {selectedNode.line ? `:${selectedNode.line}` : ""}
              </div>
            )}
            <div>
              <span className="muted">Internal ID:</span>{" "}
              <code>{selectedNode.id}</code>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

import { useState } from "react";
import type { FeatureTraceV1, TraceNodeV1 } from "./types";

interface Props {
  trace: FeatureTraceV1;
}

export function FeatureTraceGraph({ trace }: Props) {
  const [selectedNode, setSelectedNode] = useState<TraceNodeV1 | null>(null);

  if (!trace.nodes || trace.nodes.length === 0) {
    return (
      <div
        style={{
          padding: "24px",
          textAlign: "center",
          color: "var(--muted)",
          fontSize: "14px",
          background: "#f8fafc",
          border: "1px dashed var(--border)",
          borderRadius: "6px",
        }}
      >
        No graph traversal paths observed for this finding within budget constraints.
      </div>
    );
  }

  // Layout calculation for simple DAG presentation
  // Group nodes by approximate depth / type
  const rootNode = trace.nodes.find((n) => n.id === trace.finding_id) ?? trace.nodes[0];
  const routeNodes = trace.nodes.filter(
    (n) => n.kind === "route" || trace.route_endpoints.includes(n.label),
  );
  const intermediateNodes = trace.nodes.filter(
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
          2-Hop Structural CPG Feature Trace
        </div>
        <div style={{ display: "flex", gap: "8px", fontSize: "12px" }}>
          <span className="muted">
            {trace.nodes.length} nodes · {trace.edges.length} edges
          </span>
          {trace.truncated && (
            <span
              style={{
                color: "#a55307",
                background: "#fef3c7",
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

      {trace.route_endpoints.length > 0 && (
        <div
          style={{
            padding: "8px 12px",
            background: "#eff6ff",
            border: "1px solid #bfdbfe",
            borderRadius: "6px",
            fontSize: "12px",
            color: "#1e40af",
            marginBottom: "12px",
          }}
        >
          <strong>Connected Route Endpoints:</strong>{" "}
          {trace.route_endpoints.join(", ")}
        </div>
      )}

      {/* Interactive Visual Graph Canvas */}
      <div
        style={{
          background: "#f8fafc",
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
              Finding Source (Hop 0)
            </div>
            {rootNode && (
              <div
                onClick={() => setSelectedNode(rootNode)}
                style={{
                  padding: "10px 12px",
                  background:
                    selectedNode?.id === rootNode.id ? "#e0e7ff" : "#fee2e2",
                  border:
                    selectedNode?.id === rootNode.id
                      ? "2px solid #4338ca"
                      : "1px solid #fca5a5",
                  borderRadius: "6px",
                  cursor: "pointer",
                  fontSize: "12px",
                }}
              >
                <div style={{ fontWeight: 700, color: "#991b1b" }}>
                  {rootNode.label}
                </div>
                <div
                  className="muted"
                  style={{ fontSize: "11px", marginTop: "4px" }}
                >
                  {rootNode.file_path}
                  {rootNode.line_number ? `:${rootNode.line_number}` : ""}
                </div>
              </div>
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
              Connected Nodes (Hop 1-2)
            </div>
            <div style={{ display: "grid", gap: "8px" }}>
              {intermediateNodes.slice(0, 8).map((node) => (
                <div
                  key={node.id}
                  onClick={() => setSelectedNode(node)}
                  style={{
                    padding: "8px 10px",
                    background:
                      selectedNode?.id === node.id ? "#e0e7ff" : "var(--surface)",
                    border:
                      selectedNode?.id === node.id
                        ? "2px solid #4338ca"
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
                </div>
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
              Exposed Routes / Sinks
            </div>
            <div style={{ display: "grid", gap: "8px" }}>
              {routeNodes.length === 0 ? (
                <div
                  className="muted"
                  style={{ fontSize: "12px", fontStyle: "italic" }}
                >
                  No external routes reached within 2 hops.
                </div>
              ) : (
                routeNodes.map((node) => (
                  <div
                    key={node.id}
                    onClick={() => setSelectedNode(node)}
                    style={{
                      padding: "8px 10px",
                      background:
                        selectedNode?.id === node.id ? "#e0e7ff" : "#dbeafe",
                      border:
                        selectedNode?.id === node.id
                          ? "2px solid #4338ca"
                          : "1px solid #93c5fd",
                      borderRadius: "6px",
                      cursor: "pointer",
                      fontSize: "12px",
                    }}
                  >
                    <div style={{ fontWeight: 700, color: "#1e40af" }}>
                      {node.label}
                    </div>
                    <div
                      className="muted"
                      style={{ fontSize: "11px", marginTop: "2px" }}
                    >
                      route endpoint
                    </div>
                  </div>
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
            background: "#f1f5f9",
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
              <span className="muted">Kind:</span> <code>{selectedNode.kind}</code>
            </div>
            {selectedNode.file_path && (
              <div>
                <span className="muted">File:</span>{" "}
                <code>{selectedNode.file_path}</code>
                {selectedNode.line_number ? `:${selectedNode.line_number}` : ""}
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

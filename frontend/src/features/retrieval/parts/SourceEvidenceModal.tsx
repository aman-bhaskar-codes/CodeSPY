import { Modal } from "../../../ui/Modal";
import { useRetrieval } from "../retrievalContext";

/** The source evidence drawer for a selected hit. */
export function SourceEvidenceModal() {
  const {
    sourceViewQuery,
    selectedFileForView: maybeSelectedFileForView,
    setSelectedFileForView,
  } = useRetrieval();
  if (!maybeSelectedFileForView) return null;
  const selectedFileForView = maybeSelectedFileForView;
  return (
    <>
      <Modal
        open
        title="Source evidence"
        onClose={() => setSelectedFileForView(null)}
        variant="drawer"
      >
        <div
          style={{
            background: "var(--cs-panel)",
            borderRadius: 8,
            maxWidth: 900,
            width: "100%",
            maxHeight: "90vh",
            display: "flex",
            flexDirection: "column",
            padding: 20,
            boxShadow: "var(--cs-shadow), 0 10px 10px -5px rgba(0, 0, 0, 0.04)",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: 14,
              borderBottom: "1px solid var(--border)",
              paddingBottom: 10,
            }}
          >
            <div>
              <h3 style={{ margin: 0, fontSize: 16 }}>
                Privacy-Transformed Model-Safe Source View
              </h3>
              <span
                style={{
                  fontSize: 12,
                  color: "var(--muted)",
                  fontFamily: "var(--cs-mono)",
                }}
              >
                {selectedFileForView.filePath}
                {selectedFileForView.startLine && selectedFileForView.endLine
                  ? ` #L${selectedFileForView.startLine}-L${selectedFileForView.endLine}`
                  : ""}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setSelectedFileForView(null)}
              style={{ minWidth: 60, padding: "4px 8px" }}
            >
              Close
            </button>
          </div>

          <div style={{ flex: 1, overflowY: "auto" }}>
            {sourceViewQuery.isLoading ? (
              <p className="muted">
                Extracting verified model-safe source range…
              </p>
            ) : sourceViewQuery.isError ? (
              <p role="alert" style={{ color: "var(--danger)" }}>
                Failed to load source view:{" "}
                {(sourceViewQuery.error as Error).message}
              </p>
            ) : sourceViewQuery.data ? (
              <div>
                <div
                  style={{
                    display: "flex",
                    gap: 16,
                    fontSize: 12,
                    color: "var(--muted)",
                    marginBottom: 10,
                  }}
                >
                  <span>
                    Representation: {sourceViewQuery.data.representation}
                  </span>
                  <span>
                    Policy Version: {sourceViewQuery.data.policy_version}
                  </span>
                  <span>
                    Redactions:{" "}
                    {
                      (sourceViewQuery.data.interval_mappings ?? []).filter(
                        (m) => m.is_redacted,
                      ).length
                    }
                  </span>
                  <span>
                    SHA: {sourceViewQuery.data.content_sha256.slice(0, 16)}…
                  </span>
                </div>

                {sourceViewQuery.data.is_omitted ? (
                  <p
                    style={{
                      color: "var(--cs-warning)",
                      background: "var(--cs-warning-bg)",
                      padding: 12,
                      borderRadius: 4,
                    }}
                  >
                    File content omitted: {sourceViewQuery.data.omission_reason}
                  </p>
                ) : (
                  <pre
                    style={{
                      background: "var(--cs-panel)",
                      color: "var(--cs-text)",
                      padding: 14,
                      borderRadius: 6,
                      overflowX: "auto",
                      fontSize: 13,
                      lineHeight: 1.5,
                      margin: 0,
                    }}
                  >
                    <code>{sourceViewQuery.data.content_text}</code>
                  </pre>
                )}
              </div>
            ) : null}
          </div>
        </div>
      </Modal>
    </>
  );
}

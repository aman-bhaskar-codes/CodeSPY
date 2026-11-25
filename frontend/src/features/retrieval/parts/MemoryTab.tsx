import * as Tabs from "@radix-ui/react-tabs";
import { MemoryReviewPanel } from "../MemoryReviewPanel";
import type { MemoryEntryV1, MemoryKind } from "../types";
import { useRetrieval } from "../retrievalContext";

/** Repository memory: proposals, review and pinned evidence. */
export function MemoryTab() {
  const {
    repositoryId,
    snapshotId,
    csrfToken,
    memoriesQuery,
    recordMemoryMutation,
    setMemoryQuery,
    memoryMode,
    setMemoryMode,
    memoryDraft,
    setMemoryDraft,
    newMemoryTopic,
    setNewMemoryTopic,
    newMemoryKind,
    setNewMemoryKind,
    newMemorySummary,
    setNewMemorySummary,
    newMemoryEvidenceFile,
    setNewMemoryEvidenceFile,
    newMemoryStartLine,
    setNewMemoryStartLine,
    newMemoryEndLine,
    setNewMemoryEndLine,
    newMemorySymbol,
    setNewMemorySymbol,
    memorySuccessMsg,
    memoryValidation,
    setMemoryValidation,
    pinnedEvidence,
    setPinnedEvidence,
  } = useRetrieval();
  return (
    <>
      <Tabs.Content value="memory" asChild>
        <div>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 12,
              marginBottom: 18,
            }}
          >
            <input
              type="text"
              placeholder="Query repository memories by topic or semantic phrase…"
              value={memoryDraft}
              onChange={(e) => setMemoryDraft(e.target.value)}
              style={{ flex: "1 1 180px", minWidth: 0 }}
            />
            <button
              type="button"
              className="primary"
              onClick={() => setMemoryQuery(memoryDraft)}
            >
              Filter Memories
            </button>
            {memoryDraft && (
              <button
                type="button"
                onClick={() => {
                  setMemoryDraft("");
                  setMemoryQuery("");
                }}
              >
                Clear memory query
              </button>
            )}
          </div>

          <label>
            Memory scope
            <select
              value={memoryMode}
              onChange={(event) =>
                setMemoryMode(event.target.value as typeof memoryMode)
              }
            >
              <option value="off">Memory off</option>
              <option value="current_only">Current snapshot</option>
              <option value="historical_hints">Include historical hints</option>
            </select>
          </label>
          <MemoryReviewPanel
            repositoryId={repositoryId}
            snapshotId={snapshotId}
            csrfToken={csrfToken}
          />
          {memoriesQuery.data?.partial && (
            <p role="status">
              Memory selection reached its validation limit; coverage is
              partial.
            </p>
          )}
          {!!memoriesQuery.data?.omission_reasons?.length && (
            <p>Omitted: {memoriesQuery.data.omission_reasons.join(", ")}</p>
          )}
          {memorySuccessMsg && (
            <p
              style={{
                color: "var(--cs-success)",
                fontWeight: 600,
                marginBottom: 12,
              }}
            >
              ✓ {memorySuccessMsg}
            </p>
          )}

          {memoryValidation && <p role="alert">{memoryValidation}</p>}
          {/* Record New Grounded Memory Box */}
          <div
            style={{
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: 16,
              background: "var(--cs-panel)",
              marginBottom: 20,
            }}
          >
            <h3 style={{ margin: "0 0 12px", fontSize: 16 }}>
              Record Grounded Memory
            </h3>
            {pinnedEvidence && (
              <p className="muted">
                Pinned evidence set:{" "}
                <code style={{ overflowWrap: "anywhere" }}>
                  {pinnedEvidence.chunk_set_id}
                </code>
                . Editing its file or range removes the chunk association.
              </p>
            )}
            <form
              noValidate
              onSubmit={(e) => {
                e.preventDefault();
                if (!newMemoryTopic.trim() || !newMemorySummary.trim()) {
                  setMemoryValidation("Enter a memory topic and summary.");
                  return;
                }
                if (
                  newMemoryEvidenceFile &&
                  ((newMemoryStartLine ?? 1) < 1 ||
                    (newMemoryEndLine ?? newMemoryStartLine ?? 1) <
                      (newMemoryStartLine ?? 1))
                ) {
                  setMemoryValidation(
                    "Enter a valid evidence line range, starting at line 1 or later.",
                  );
                  return;
                }
                setMemoryValidation("");
                recordMemoryMutation.mutate({
                  memory_kind: newMemoryKind,
                  topic: newMemoryTopic,
                  summary: newMemorySummary,
                  evidence_pointers: pinnedEvidence
                    ? [pinnedEvidence]
                    : newMemoryEvidenceFile
                      ? [
                          {
                            file_path: newMemoryEvidenceFile,
                            start_line: newMemoryStartLine ?? 1,
                            end_line:
                              newMemoryEndLine ?? newMemoryStartLine ?? 1,
                            symbol_name: newMemorySymbol || undefined,
                          },
                        ]
                      : [],
                });
              }}
              style={{ display: "grid", gap: 10, maxWidth: "100%" }}
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: 12,
                }}
              >
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>
                    Memory Topic / Identifier:
                  </label>
                  <input
                    type="text"
                    required
                    placeholder="e.g. auth-timing-invariance"
                    value={newMemoryTopic}
                    onChange={(e) => setNewMemoryTopic(e.target.value)}
                    style={{ width: "100%" }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>
                    Memory Classification Kind:
                  </label>
                  <select
                    value={newMemoryKind}
                    onChange={(e) =>
                      setNewMemoryKind(e.target.value as MemoryKind)
                    }
                    style={{ width: "100%" }}
                  >
                    <option value="architecture_fact">Architecture Fact</option>
                    <option value="security_finding_evidence">
                      Security Finding Evidence
                    </option>
                    <option value="triage_resolution">Triage Resolution</option>
                  </select>
                </div>
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600 }}>
                  Grounded Summary / Finding Resolution:
                </label>
                <textarea
                  className="resize-none"
                  required
                  rows={2}
                  placeholder="Describe the observation to propose for review..."
                  value={newMemorySummary}
                  onChange={(e) => setNewMemorySummary(e.target.value)}
                  style={{
                    minHeight: 80,
                    width: "100%",
                    padding: 8,
                    border: "1px solid var(--border)",
                    borderRadius: 4,
                    fontFamily: "inherit",
                  }}
                />
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
                  gap: 8,
                }}
              >
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>
                    Evidence File:
                  </label>
                  <input
                    type="text"
                    placeholder="src/auth.py"
                    value={newMemoryEvidenceFile}
                    onChange={(e) => {
                      setNewMemoryEvidenceFile(e.target.value);
                      setPinnedEvidence(null);
                    }}
                    style={{ width: "100%" }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>
                    Start Line:
                  </label>
                  <input
                    type="number"
                    value={newMemoryStartLine ?? ""}
                    onChange={(e) => (
                      setNewMemoryStartLine(
                        e.target.value ? Number(e.target.value) : undefined,
                      ),
                      setPinnedEvidence(null)
                    )}
                    style={{ width: "100%" }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>
                    End Line:
                  </label>
                  <input
                    type="number"
                    value={newMemoryEndLine ?? ""}
                    onChange={(e) => (
                      setNewMemoryEndLine(
                        e.target.value ? Number(e.target.value) : undefined,
                      ),
                      setPinnedEvidence(null)
                    )}
                    style={{ width: "100%" }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>
                    Symbol:
                  </label>
                  <input
                    type="text"
                    placeholder="login_user"
                    value={newMemorySymbol}
                    onChange={(e) => setNewMemorySymbol(e.target.value)}
                    style={{ width: "100%" }}
                  />
                </div>
              </div>

              <button
                type="submit"
                className="primary"
                disabled={
                  recordMemoryMutation.isPending ||
                  !newMemoryTopic ||
                  !newMemorySummary
                }
                style={{ width: 200, marginTop: 4 }}
              >
                {recordMemoryMutation.isPending
                  ? "Recording…"
                  : "Submit memory for review"}
              </button>
            </form>
          </div>

          {/* List Memories */}
          <div>
            <h3 style={{ margin: "0 0 12px", fontSize: 16 }}>
              Repository Memories
            </h3>
            {memoriesQuery.isLoading ? (
              <p className="muted">Loading memories…</p>
            ) : memoriesQuery.data?.items.length === 0 ? (
              <p className="muted">
                No eligible reviewed memories recorded for this repository yet.
              </p>
            ) : (
              <div style={{ display: "grid", gap: 10 }}>
                {memoriesQuery.data?.items.map((mem: MemoryEntryV1) => (
                  <div
                    key={mem.memory_id}
                    style={{
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: 12,
                      background: "var(--cs-panel)",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        marginBottom: 6,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                        }}
                      >
                        <span style={{ fontWeight: 700, fontSize: 14 }}>
                          {mem.topic}
                        </span>
                        <span
                          style={{
                            background: "var(--cs-info-bg)",
                            color: "var(--cs-action)",
                            fontSize: 11,
                            fontWeight: 600,
                            padding: "2px 6px",
                            borderRadius: 3,
                          }}
                        >
                          {mem.memory_kind} ·{" "}
                          {mem.historical_hint
                            ? "Historical hint — revalidate current source"
                            : "Current snapshot"}
                          {mem.source_correspondence === "human_note" &&
                            " · Human note; not source evidence"}
                        </span>
                      </div>
                      <span style={{ fontSize: 12, color: "var(--muted)" }}>
                        Recorded {new Date(mem.created_at).toLocaleDateString()}
                      </span>
                    </div>

                    <p style={{ margin: "4px 0 8px", fontSize: 14 }}>
                      {mem.summary}
                    </p>

                    {(mem.evidence_pointers ?? []).length > 0 && (
                      <div
                        style={{
                          display: "flex",
                          gap: 8,
                          flexWrap: "wrap",
                          alignItems: "center",
                        }}
                      >
                        <span style={{ fontSize: 11, color: "var(--muted)" }}>
                          Evidence:
                        </span>
                        {(mem.evidence_pointers ?? []).map((ev, i) => (
                          <span
                            key={i}
                            style={{
                              fontFamily: "var(--cs-mono)",
                              fontSize: 12,
                              background: "var(--cs-raised)",
                              padding: "1px 6px",
                              borderRadius: 3,
                            }}
                          >
                            {ev.file_path}
                            {ev.start_line && ev.end_line
                              ? `#L${ev.start_line}-L${ev.end_line}`
                              : ""}
                            {ev.symbol_name ? ` (${ev.symbol_name})` : ""}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </Tabs.Content>
    </>
  );
}

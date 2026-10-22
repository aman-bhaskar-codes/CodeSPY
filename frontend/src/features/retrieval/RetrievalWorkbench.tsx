import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type {
  ContextItemV1,
  ContextPacketV1,
  HybridSearchHitV1,
  MemoryCreateRequestV1,
  MemoryEntryV1,
  MemoryKind,
  SearchHitV1,
  SearchMode,
} from "./types";

interface RetrievalWorkbenchProps {
  repositoryId: string;
  snapshotId: string;
  csrfToken: string;
}

export function RetrievalWorkbench({
  repositoryId,
  snapshotId,
  csrfToken,
}: RetrievalWorkbenchProps) {
  const [activeTab, setActiveTab] = useState<"search" | "context" | "memory">("search");
  const [searchMode, setSearchMode] = useState<SearchMode>("hybrid");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(10);
  const [denseIndexId, setDenseIndexId] = useState("");

  // Context Engineering states
  const [maxTokens, setMaxTokens] = useState(2048);
  const [overheadTokens, setOverheadTokens] = useState(128);
  const [preamble, setPreamble] = useState("You are an expert security software engineer assisting with CodeSPY.");
  const [lastPacket, setLastPacket] = useState<ContextPacketV1 | null>(null);

  // Source View Modal
  const [selectedFileForView, setSelectedFileForView] = useState<{
    fileId: string;
    filePath: string;
    startLine?: number;
    endLine?: number;
  } | null>(null);

  // Memory states
  const [memoryQuery, setMemoryQuery] = useState("");
  const [newMemoryTopic, setNewMemoryTopic] = useState("");
  const [newMemoryKind, setNewMemoryKind] = useState<MemoryKind>("architecture_invariant");
  const [newMemorySummary, setNewMemorySummary] = useState("");
  const [newMemoryEvidenceFile, setNewMemoryEvidenceFile] = useState("");
  const [newMemoryStartLine, setNewMemoryStartLine] = useState<number | undefined>();
  const [newMemoryEndLine, setNewMemoryEndLine] = useState<number | undefined>();
  const [newMemorySymbol, setNewMemorySymbol] = useState("");
  const [memorySuccessMsg, setMemorySuccessMsg] = useState("");

  // Search Mutation
  const searchMutation = useMutation({
    mutationFn: async () => {
      if (!query.trim()) return null;
      if (searchMode === "exact") {
        return await api.searchExact(repositoryId, snapshotId, query, limit, csrfToken);
      }
      if (searchMode === "lexical") {
        return await api.searchLexical(repositoryId, snapshotId, query, limit, csrfToken);
      }
      if (searchMode === "dense") {
        return await api.searchDense(
          repositoryId,
          snapshotId,
          query,
          denseIndexId || undefined,
          limit,
          csrfToken,
        );
      }
      // Hybrid
      return await api.searchHybrid(
        repositoryId,
        snapshotId,
        query,
        limit,
        denseIndexId || undefined,
        csrfToken,
      );
    },
  });

  // Context Assembly Mutation
  const assembleMutation = useMutation({
    mutationFn: async () => {
      const hits = searchMutation.data && "hits" in searchMutation.data
        ? (searchMutation.data.hits as HybridSearchHitV1[])
        : undefined;
      const res = await api.assembleContext(
        repositoryId,
        snapshotId,
        {
          query: query || "CodeSPY retrieval inquiry",
          candidate_hits: hits,
          budget: {
            max_tokens: maxTokens,
            reserved_overhead_tokens: overheadTokens,
          },
          system_preamble: preamble || undefined,
        },
        csrfToken,
      );
      setLastPacket(res);
      return res;
    },
  });

  // Memory Query
  const memoriesQuery = useQuery({
    queryKey: ["repo_memories", repositoryId, snapshotId, memoryQuery],
    queryFn: ({ signal }) =>
      api.queryMemories(repositoryId, snapshotId, { query: memoryQuery || undefined }, signal),
  });

  // Memory Create Mutation
  const recordMemoryMutation = useMutation({
    mutationFn: async (payload: MemoryCreateRequestV1) => {
      return await api.recordMemory(repositoryId, snapshotId, payload, csrfToken);
    },
    onSuccess: () => {
      setMemorySuccessMsg("Grounded memory recorded successfully!");
      setNewMemoryTopic("");
      setNewMemorySummary("");
      setNewMemoryEvidenceFile("");
      setNewMemoryStartLine(undefined);
      setNewMemoryEndLine(undefined);
      setNewMemorySymbol("");
      memoriesQuery.refetch();
      setTimeout(() => setMemorySuccessMsg(""), 4000);
    },
  });

  // Source View Query
  const sourceViewQuery = useQuery({
    queryKey: [
      "source_view",
      repositoryId,
      snapshotId,
      selectedFileForView?.fileId,
      selectedFileForView?.startLine,
      selectedFileForView?.endLine,
    ],
    queryFn: ({ signal }) => {
      if (!selectedFileForView) return null;
      return api.sourceView(
        repositoryId,
        snapshotId,
        selectedFileForView.fileId,
        {
          start_line: selectedFileForView.startLine,
          end_line: selectedFileForView.endLine,
          representation: "model_safe",
        },
        signal,
      );
    },
    enabled: !!selectedFileForView,
  });

  const handlePinAsMemory = (hit: SearchHitV1 | HybridSearchHitV1) => {
    setActiveTab("memory");
    setNewMemoryTopic(hit.symbol_name ?? hit.file_path);
    setNewMemorySummary(`Evidence identified in ${hit.file_path} via retrieval query '${query}'.`);
    setNewMemoryEvidenceFile(hit.file_path);
    setNewMemoryStartLine(hit.start_line);
    setNewMemoryEndLine(hit.end_line);
    setNewMemorySymbol(hit.symbol_name ?? "");
  };

  const copyCitation = (citation: string) => {
    navigator.clipboard.writeText(citation);
  };

  return (
    <section aria-labelledby="retrieval-title" style={{ marginTop: 24 }}>
      <div className="section-heading" style={{ marginBottom: 18 }}>
        <div>
          <h2 id="retrieval-title">Source Retrieval & Grounding Workbench</h2>
          <span className="muted">
            Multi-modal hybrid search (AST + BM25 + Titan V2 Dense), token-budgeted context & evidence memory
          </span>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            type="button"
            className={activeTab === "search" ? "primary" : ""}
            onClick={() => setActiveTab("search")}
          >
            Multi-Modal Search
          </button>
          <button
            type="button"
            className={activeTab === "context" ? "primary" : ""}
            onClick={() => setActiveTab("context")}
          >
            Context Engineering
          </button>
          <button
            type="button"
            className={activeTab === "memory" ? "primary" : ""}
            onClick={() => setActiveTab("memory")}
          >
            Repository Memory
          </button>
        </div>
      </div>

      {/* TAB 1: MULTI-MODAL SEARCH */}
      {activeTab === "search" && (
        <div>
          <div
            style={{
              display: "flex",
              gap: 12,
              flexWrap: "wrap",
              alignItems: "center",
              marginBottom: 16,
              padding: 12,
              background: "#e8effc",
              borderRadius: 6,
            }}
          >
            <span style={{ fontWeight: 600, fontSize: 13, textTransform: "uppercase" }}>Channel:</span>
            {(["hybrid", "lexical", "dense", "exact"] as const).map((m) => (
              <label
                key={m}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  cursor: "pointer",
                  fontSize: 14,
                  fontWeight: searchMode === m ? 700 : 400,
                }}
              >
                <input
                  type="radio"
                  name="searchMode"
                  value={m}
                  checked={searchMode === m}
                  onChange={() => setSearchMode(m)}
                />
                {m === "hybrid"
                  ? "Hybrid (RRF Fusion)"
                  : m === "lexical"
                  ? "Lexical (BM25)"
                  : m === "dense"
                  ? "Dense (Vector)"
                  : "Exact (AST/Symbol)"}
              </label>
            ))}
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              searchMutation.mutate();
            }}
            style={{ display: "flex", gap: 12, maxWidth: "100%", marginBottom: 20 }}
          >
            <input
              type="text"
              placeholder={
                searchMode === "exact"
                  ? "Exact symbol or filepath (e.g. login_user or src/auth.py)"
                  : searchMode === "dense"
                  ? "Semantic query (e.g. authenticate user with password credentials)"
                  : "Search code, symbols, or topics..."
              }
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ flex: 1 }}
              required
            />
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              style={{ width: 100 }}
              aria-label="Result limit"
            >
              <option value={5}>Top 5</option>
              <option value={10}>Top 10</option>
              <option value={20}>Top 20</option>
              <option value={50}>Top 50</option>
            </select>
            {(searchMode === "dense" || searchMode === "hybrid") && (
              <input
                type="text"
                placeholder="Index UUID (optional)"
                value={denseIndexId}
                onChange={(e) => setDenseIndexId(e.target.value)}
                style={{ width: 170, fontSize: 13 }}
                title="Optional specific Dense Index UUID"
              />
            )}
            <button
              type="submit"
              className="primary"
              disabled={searchMutation.isPending || !query.trim()}
            >
              {searchMutation.isPending ? "Searching…" : "Search"}
            </button>
          </form>

          {searchMutation.isError && (
            <p role="alert" style={{ color: "var(--danger)" }}>
              Search failed: {(searchMutation.error as Error).message}
            </p>
          )}

          {searchMutation.data && (
            <div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 12,
                }}
              >
                <span style={{ fontSize: 13, color: "var(--muted)" }}>
                  Retrieved {searchMutation.data.hits.length} of {searchMutation.data.total_candidates} candidates
                  in {searchMutation.data.execution_ms}ms
                </span>
                <button
                  type="button"
                  style={{ fontSize: 13, padding: "4px 10px" }}
                  onClick={() => {
                    setActiveTab("context");
                    assembleMutation.mutate();
                  }}
                >
                  Pack Top Hits into Context Packet →
                </button>
              </div>

              {searchMutation.data.hits.length === 0 ? (
                <p className="muted" style={{ padding: 24, textAlign: "center", border: "1px dashed var(--border)" }}>
                  No matching source chunks found for this query within the authorized scope.
                </p>
              ) : (
                <div style={{ display: "grid", gap: 12 }}>
                  {searchMutation.data.hits.map((hit: SearchHitV1 | HybridSearchHitV1) => {
                    const isHybrid = "match_modalities" in hit;
                    const hybridHit = isHybrid ? (hit as HybridSearchHitV1) : null;
                    return (
                      <div
                        key={hit.chunk_id}
                        style={{
                          border: "1px solid var(--border)",
                          borderRadius: 6,
                          padding: 14,
                          background: "#fff",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            justifyContent: "space-between",
                            alignItems: "center",
                            marginBottom: 8,
                          }}
                        >
                          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                            <span
                              style={{
                                fontWeight: 700,
                                fontFamily: "monospace",
                                fontSize: 14,
                                color: "var(--ink)",
                              }}
                            >
                              {hit.file_path}
                              {hit.start_line && hit.end_line
                                ? ` #L${hit.start_line}-L${hit.end_line}`
                                : ""}
                            </span>
                            {hit.symbol_name && (
                              <span
                                style={{
                                  background: "#f0f4f8",
                                  padding: "2px 8px",
                                  borderRadius: 4,
                                  fontSize: 12,
                                  fontFamily: "monospace",
                                  color: "var(--action)",
                                }}
                              >
                                {hit.symbol_name} ({hit.chunk_kind})
                              </span>
                            )}
                            {hybridHit?.match_modalities?.map((m) => (
                              <span
                                key={m}
                                style={{
                                  background: m === "exact" ? "#d1fae5" : m === "dense" ? "#ede9fe" : "#e0f2fe",
                                  color: m === "exact" ? "#065f46" : m === "dense" ? "#5b21b6" : "#075985",
                                  fontSize: 11,
                                  fontWeight: 600,
                                  padding: "1px 6px",
                                  borderRadius: 3,
                                  textTransform: "uppercase",
                                }}
                              >
                                {m}
                              </span>
                            ))}
                          </div>
                          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)" }}>
                              Score: {hit.score.toFixed(4)}
                            </span>
                            <button
                              type="button"
                              style={{ padding: "3px 8px", fontSize: 12 }}
                              onClick={() =>
                                setSelectedFileForView({
                                  fileId: hit.file_id,
                                  filePath: hit.file_path,
                                  startLine: hit.start_line,
                                  endLine: hit.end_line,
                                })
                              }
                            >
                              Safe Source View
                            </button>
                            <button
                              type="button"
                              style={{ padding: "3px 8px", fontSize: 12 }}
                              onClick={() => handlePinAsMemory(hit)}
                            >
                              Pin to Memory
                            </button>
                          </div>
                        </div>

                        {hybridHit && Object.keys(hybridHit.modality_ranks).length > 0 && (
                          <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 8 }}>
                            Channel Ranks:{" "}
                            {Object.entries(hybridHit.modality_ranks)
                              .map(([k, r]) => `${k}: #${r}`)
                              .join(" | ")}
                          </div>
                        )}

                        <pre
                          style={{
                            background: "#0f172a",
                            color: "#e2e8f0",
                            padding: 12,
                            borderRadius: 4,
                            overflowX: "auto",
                            fontSize: 13,
                            lineHeight: 1.45,
                            margin: 0,
                          }}
                        >
                          <code>{hit.snippet}</code>
                        </pre>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* TAB 2: CONTEXT ENGINEERING */}
      {activeTab === "context" && (
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
            <div>
              <label style={{ display: "block", marginBottom: 6, fontSize: 13 }}>
                Max Token Budget ({maxTokens} tokens):
              </label>
              <input
                type="range"
                min={256}
                max={8192}
                step={256}
                value={maxTokens}
                onChange={(e) => setMaxTokens(Number(e.target.value))}
                style={{ width: "100%" }}
              />
            </div>
            <div>
              <label style={{ display: "block", marginBottom: 6, fontSize: 13 }}>
                Reserved Overhead Tokens ({overheadTokens} tokens):
              </label>
              <input
                type="range"
                min={0}
                max={512}
                step={32}
                value={overheadTokens}
                onChange={(e) => setOverheadTokens(Number(e.target.value))}
                style={{ width: "100%" }}
              />
            </div>
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 6, fontSize: 13 }}>
              LLM System Preamble / Grounding Fence:
            </label>
            <textarea
              rows={2}
              value={preamble}
              onChange={(e) => setPreamble(e.target.value)}
              style={{
                width: "100%",
                padding: 10,
                border: "1px solid var(--border)",
                borderRadius: 4,
                fontFamily: "inherit",
              }}
            />
          </div>

          <button
            type="button"
            className="primary"
            disabled={assembleMutation.isPending}
            onClick={() => assembleMutation.mutate()}
            style={{ marginBottom: 18 }}
          >
            {assembleMutation.isPending ? "Assembling Grounded Context…" : "Assemble Grounded Context Packet"}
          </button>

          {lastPacket && (
            <div>
              <div
                style={{
                  background: "#f8fafc",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: 16,
                  marginBottom: 16,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <span style={{ fontWeight: 700, fontSize: 14 }}>
                    Grounding Packet: {lastPacket.items_count} items packed ({lastPacket.total_tokens} / {maxTokens} tokens)
                  </span>
                  <span style={{ fontSize: 12, fontFamily: "monospace", color: "var(--muted)" }}>
                    Digest: {lastPacket.packet_sha256.slice(0, 16)}…
                  </span>
                </div>

                {/* Token Meter */}
                <div
                  style={{
                    height: 8,
                    background: "#e2e8f0",
                    borderRadius: 4,
                    overflow: "hidden",
                    marginBottom: 12,
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: `${Math.min(100, (lastPacket.total_tokens / maxTokens) * 100)}%`,
                      background: lastPacket.total_tokens > maxTokens * 0.9 ? "#ef4444" : "var(--action)",
                    }}
                  />
                </div>

                <div style={{ fontSize: 13, color: "var(--muted)" }}>
                  Total Bytes: {lastPacket.total_bytes} bytes | Omitted: {lastPacket.omitted_count} items
                </div>
              </div>

              <div style={{ display: "grid", gap: 12 }}>
                {lastPacket.items.map((item: ContextItemV1) => (
                  <div
                    key={item.chunk_id}
                    style={{
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: 14,
                      background: "#fff",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        marginBottom: 8,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <button
                          type="button"
                          onClick={() => copyCitation(item.citation_key)}
                          style={{
                            padding: "2px 8px",
                            fontSize: 12,
                            fontWeight: 700,
                            fontFamily: "monospace",
                            background: "#e0e7ff",
                            color: "#3730a3",
                            borderRadius: 4,
                            cursor: "pointer",
                          }}
                          title="Click to copy citation tag"
                        >
                          {item.citation_key} (copy)
                        </button>
                        <span style={{ fontWeight: 600, fontSize: 13 }}>
                          {item.file_path}:{item.start_line}-{item.end_line}
                        </span>
                        {item.symbol_name && (
                          <span className="muted" style={{ fontSize: 12 }}>
                            ({item.symbol_name})
                          </span>
                        )}
                      </div>
                      <span style={{ fontSize: 12, color: "var(--muted)" }}>
                        {item.token_count} tokens | score: {item.relevance_score.toFixed(4)}
                      </span>
                    </div>

                    <pre
                      style={{
                        background: "#0f172a",
                        color: "#e2e8f0",
                        padding: 12,
                        borderRadius: 4,
                        overflowX: "auto",
                        fontSize: 13,
                        lineHeight: 1.45,
                        margin: 0,
                      }}
                    >
                      <code>{item.safe_content}</code>
                    </pre>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* TAB 3: REPOSITORY MEMORY */}
      {activeTab === "memory" && (
        <div>
          <div style={{ display: "flex", gap: 12, marginBottom: 18 }}>
            <input
              type="text"
              placeholder="Query repository memories by topic or semantic phrase…"
              value={memoryQuery}
              onChange={(e) => setMemoryQuery(e.target.value)}
              style={{ flex: 1 }}
            />
            <button
              type="button"
              className="primary"
              onClick={() => memoriesQuery.refetch()}
            >
              Filter Memories
            </button>
          </div>

          {memorySuccessMsg && (
            <p style={{ color: "#059669", fontWeight: 600, marginBottom: 12 }}>
              ✓ {memorySuccessMsg}
            </p>
          )}

          {/* Record New Grounded Memory Box */}
          <div
            style={{
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: 16,
              background: "#fdfefe",
              marginBottom: 20,
            }}
          >
            <h3 style={{ margin: "0 0 12px", fontSize: 16 }}>Record Grounded Memory</h3>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                recordMemoryMutation.mutate({
                  memory_kind: newMemoryKind,
                  topic: newMemoryTopic,
                  summary: newMemorySummary,
                  evidence_pointers: newMemoryEvidenceFile
                    ? [
                        {
                          file_path: newMemoryEvidenceFile,
                          start_line: newMemoryStartLine,
                          end_line: newMemoryEndLine,
                          symbol_name: newMemorySymbol || undefined,
                        },
                      ]
                    : [],
                });
              }}
              style={{ display: "grid", gap: 10, maxWidth: "100%" }}
            >
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Memory Topic / Identifier:</label>
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
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Memory Classification Kind:</label>
                  <select
                    value={newMemoryKind}
                    onChange={(e) => setNewMemoryKind(e.target.value as MemoryKind)}
                    style={{ width: "100%" }}
                  >
                    <option value="architecture_invariant">Architecture Invariant</option>
                    <option value="security_finding_investigation">Security Investigation</option>
                    <option value="triage_resolution">Triage Resolution</option>
                    <option value="developer_hypothesis">Developer Hypothesis</option>
                    <option value="review_consensus">Review Consensus</option>
                  </select>
                </div>
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600 }}>Grounded Summary / Finding Resolution:</label>
                <textarea
                  required
                  rows={2}
                  placeholder="Record deterministic fact or architectural invariant established during analysis..."
                  value={newMemorySummary}
                  onChange={(e) => setNewMemorySummary(e.target.value)}
                  style={{
                    width: "100%",
                    padding: 8,
                    border: "1px solid var(--border)",
                    borderRadius: 4,
                    fontFamily: "inherit",
                  }}
                />
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr", gap: 8 }}>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Evidence File:</label>
                  <input
                    type="text"
                    placeholder="src/auth.py"
                    value={newMemoryEvidenceFile}
                    onChange={(e) => setNewMemoryEvidenceFile(e.target.value)}
                    style={{ width: "100%" }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Start Line:</label>
                  <input
                    type="number"
                    value={newMemoryStartLine ?? ""}
                    onChange={(e) =>
                      setNewMemoryStartLine(e.target.value ? Number(e.target.value) : undefined)
                    }
                    style={{ width: "100%" }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>End Line:</label>
                  <input
                    type="number"
                    value={newMemoryEndLine ?? ""}
                    onChange={(e) =>
                      setNewMemoryEndLine(e.target.value ? Number(e.target.value) : undefined)
                    }
                    style={{ width: "100%" }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Symbol:</label>
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
                disabled={recordMemoryMutation.isPending || !newMemoryTopic || !newMemorySummary}
                style={{ width: 200, marginTop: 4 }}
              >
                {recordMemoryMutation.isPending ? "Recording…" : "Save Memory"}
              </button>
            </form>
          </div>

          {/* List Memories */}
          <div>
            <h3 style={{ margin: "0 0 12px", fontSize: 16 }}>Repository Memories</h3>
            {memoriesQuery.isLoading ? (
              <p className="muted">Loading memories…</p>
            ) : memoriesQuery.data?.items.length === 0 ? (
              <p className="muted">No grounded memories recorded for this repository yet.</p>
            ) : (
              <div style={{ display: "grid", gap: 10 }}>
                {memoriesQuery.data?.items.map((mem: MemoryEntryV1) => (
                  <div
                    key={mem.memory_id}
                    style={{
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: 12,
                      background: "#fff",
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
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={{ fontWeight: 700, fontSize: 14 }}>{mem.topic}</span>
                        <span
                          style={{
                            background: "#ede9fe",
                            color: "#5b21b6",
                            fontSize: 11,
                            fontWeight: 600,
                            padding: "2px 6px",
                            borderRadius: 3,
                          }}
                        >
                          {mem.memory_kind}
                        </span>
                      </div>
                      <span style={{ fontSize: 12, color: "var(--muted)" }}>
                        Recorded {new Date(mem.created_at).toLocaleDateString()}
                      </span>
                    </div>

                    <p style={{ margin: "4px 0 8px", fontSize: 14 }}>{mem.summary}</p>

                    {mem.evidence_pointers.length > 0 && (
                      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                        <span style={{ fontSize: 11, color: "var(--muted)" }}>Evidence:</span>
                        {mem.evidence_pointers.map((ev, i) => (
                          <span
                            key={i}
                            style={{
                              fontFamily: "monospace",
                              fontSize: 12,
                              background: "#f1f5f9",
                              padding: "1px 6px",
                              borderRadius: 3,
                            }}
                          >
                            {ev.file_path}
                            {ev.start_line && ev.end_line ? `#L${ev.start_line}-L${ev.end_line}` : ""}
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
      )}

      {/* SOURCE VIEW MODAL */}
      {selectedFileForView && (
        <div
          role="dialog"
          aria-modal="true"
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: "rgba(15, 23, 42, 0.6)",
            backdropFilter: "blur(4px)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
            padding: 24,
          }}
        >
          <div
            style={{
              background: "#fff",
              borderRadius: 8,
              maxWidth: 900,
              width: "100%",
              maxHeight: "90vh",
              display: "flex",
              flexDirection: "column",
              padding: 20,
              boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)",
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
                <span style={{ fontSize: 12, color: "var(--muted)", fontFamily: "monospace" }}>
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
                <p className="muted">Extracting verified model-safe source range…</p>
              ) : sourceViewQuery.isError ? (
                <p role="alert" style={{ color: "var(--danger)" }}>
                  Failed to load source view: {(sourceViewQuery.error as Error).message}
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
                    <span>Representation: {sourceViewQuery.data.representation}</span>
                    <span>Policy Version: {sourceViewQuery.data.policy_version}</span>
                    <span>Redactions: {sourceViewQuery.data.interval_mappings.filter((m) => m.is_redacted).length}</span>
                    <span>SHA: {sourceViewQuery.data.content_sha256.slice(0, 16)}…</span>
                  </div>

                  {sourceViewQuery.data.is_omitted ? (
                    <p style={{ color: "#b45309", background: "#fef3c7", padding: 12, borderRadius: 4 }}>
                      File content omitted: {sourceViewQuery.data.omission_reason}
                    </p>
                  ) : (
                    <pre
                      style={{
                        background: "#0f172a",
                        color: "#e2e8f0",
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
        </div>
      )}
    </section>
  );
}

import * as Tabs from "@radix-ui/react-tabs";
import { Modal } from "../../ui/Modal";
import { InlineNotice } from "../../ui/InlineNotice";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { IndexBuildPanel } from "./IndexBuildPanel";
import { MemoryReviewPanel } from "./MemoryReviewPanel";
import type {
  ContextItemV1,
  EvidencePointerV1,
  SearchResponseV1,
  DenseSearchResponseV1,
  HybridSearchResponseV1,
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

export function RetrievalWorkbench(props: RetrievalWorkbenchProps) {
  return (
    <RetrievalWorkbenchState
      key={`${props.repositoryId}:${props.snapshotId}`}
      {...props}
    />
  );
}

function RetrievalWorkbenchState({
  repositoryId,
  snapshotId,
  csrfToken,
}: RetrievalWorkbenchProps) {
  const queryClient = useQueryClient();
  const proposalCommand = useRef<{ body: string; key: string } | null>(null);
  const [activeTab, setActiveTab] = useState<"search" | "context" | "memory">(
    "search",
  );
  const [searchMode, setSearchMode] = useState<SearchMode>("hybrid");
  const [query, setQuery] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  const composingSearch = useRef(false);
  const [limit, setLimit] = useState(10);
  const [denseIndexId, setDenseIndexId] = useState("");

  // Context states
  const [maxTokens, setMaxTokens] = useState(2048);
  const [overheadTokens, setOverheadTokens] = useState(128);
  const [preamble, setPreamble] = useState(
    "You are an expert security software engineer assisting with CodeSPY.",
  );
  const [includeMemory, setIncludeMemory] = useState(false);
  const [preview, setPreview] = useState<
    import("../../api/generated/types.gen").ContextPreviewV1 | null
  >(null);
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
  const [memoryMode, setMemoryMode] = useState<
    "off" | "current_only" | "historical_hints"
  >("current_only");
  const [memoryDraft, setMemoryDraft] = useState("");
  const [newMemoryTopic, setNewMemoryTopic] = useState("");
  const [newMemoryKind, setNewMemoryKind] =
    useState<MemoryKind>("architecture_fact");
  const [newMemorySummary, setNewMemorySummary] = useState("");
  const [newMemoryEvidenceFile, setNewMemoryEvidenceFile] = useState("");
  const [newMemoryStartLine, setNewMemoryStartLine] = useState<
    number | undefined
  >();
  const [newMemoryEndLine, setNewMemoryEndLine] = useState<
    number | undefined
  >();
  const [newMemorySymbol, setNewMemorySymbol] = useState("");
  const [memorySuccessMsg, setMemorySuccessMsg] = useState("");
  const [memoryValidation, setMemoryValidation] = useState("");

  const [lastSearch, setLastSearch] = useState<
    SearchResponseV1 | DenseSearchResponseV1 | HybridSearchResponseV1 | null
  >(null);
  const [pinnedEvidence, setPinnedEvidence] =
    useState<EvidencePointerV1 | null>(null);
  const searchSequence = useRef(0);
  const assembleSequence = useRef(0);

  // Search Mutation
  const searchMutation = useMutation({
    mutationFn: async () => {
      if (!query.trim()) return null;
      const seq = ++searchSequence.current;
      let result;
      if (searchMode === "exact") {
        result = await api.searchExact(
          repositoryId,
          snapshotId,
          query,
          limit,
          csrfToken,
        );
      } else if (searchMode === "lexical") {
        result = await api.searchLexical(
          repositoryId,
          snapshotId,
          query,
          limit,
          csrfToken,
        );
      } else if (searchMode === "dense") {
        result = await api.searchDense(
          repositoryId,
          snapshotId,
          query,
          denseIndexId || undefined,
          limit,
          csrfToken,
        );
      } else {
        result = await api.searchHybrid(
          repositoryId,
          snapshotId,
          query,
          limit,
          denseIndexId || undefined,
          csrfToken,
        );
      }
      return { result, seq };
    },
    onSuccess: (data) => {
      if (data && data.seq === searchSequence.current && data.result) {
        setLastSearch(data.result);
      }
    },
  });

  // Context Assembly Mutation
  const assembleMutation = useMutation({
    mutationFn: async () => {
      const seq = ++assembleSequence.current;
      const hits: HybridSearchHitV1[] | undefined = lastSearch?.hits.map(
        (hit) => ({
          ...hit,
          match_modalities:
            "match_modalities" in hit
              ? hit.match_modalities
              : ["match_type" in hit ? String(hit.match_type) : "dense"],
          modality_ranks: "modality_ranks" in hit ? hit.modality_ranks : {},
        }),
      );
      if (includeMemory) {
        const result = await api.previewContext(
          repositoryId,
          snapshotId,
          {
            query: lastSearch?.query || query || "CodeSPY retrieval inquiry",
            candidate_hits: hits,
            chunk_set_id: lastSearch?.chunk_set_id,
            budget: {
              max_tokens: maxTokens,
              reserved_overhead_tokens: overheadTokens,
            },
            memory_mode: memoryMode,
            system_preamble: preamble || undefined,
          },
          csrfToken,
        );
        return { res: result.packet, preview: result, seq };
      }
      const res = await api.assembleContext(
        repositoryId,
        snapshotId,
        {
          query: lastSearch?.query || query || "CodeSPY retrieval inquiry",
          candidate_hits: hits,
          chunk_set_id: lastSearch?.chunk_set_id,
          budget: {
            max_tokens: maxTokens,
            reserved_overhead_tokens: overheadTokens,
          },
          system_preamble: preamble || undefined,
        },
        csrfToken,
      );
      return { res, preview: null, seq };
    },
    onSuccess: (data) => {
      if (data && data.seq === assembleSequence.current && data.res) {
        setLastPacket(data.res);
        setPreview(data.preview);
      }
    },
  });

  // Memory Query
  const memoriesQuery = useQuery({
    queryKey: [
      "repo_memories",
      repositoryId,
      snapshotId,
      memoryQuery,
      memoryMode,
    ],
    queryFn: ({ signal }) =>
      api.queryMemories(
        repositoryId,
        snapshotId,
        { query: memoryQuery || undefined, history_mode: memoryMode },
        signal,
      ),
  });

  // Memory Create Mutation
  const recordMemoryMutation = useMutation({
    mutationFn: async (payload: MemoryCreateRequestV1) => {
      const body = JSON.stringify({ ...payload, request_key: undefined });
      if (proposalCommand.current?.body !== body)
        proposalCommand.current = { body, key: crypto.randomUUID() };
      const key = proposalCommand.current.key;
      return await api.recordMemory(
        repositoryId,
        snapshotId,
        { ...payload, request_key: key },
        csrfToken,
        key,
      );
    },
    onSuccess: () => {
      proposalCommand.current = null;
      void queryClient.invalidateQueries({
        queryKey: ["memory-review", repositoryId, snapshotId],
      });
      setMemorySuccessMsg(
        "Memory candidate submitted for review. It is not used by models yet.",
      );
      setNewMemoryTopic("");
      setNewMemorySummary("");
      setNewMemoryEvidenceFile("");
      setNewMemoryStartLine(undefined);
      setNewMemoryEndLine(undefined);
      setNewMemorySymbol("");
      setPinnedEvidence(null);
      memoriesQuery.refetch();
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
    setNewMemorySummary(
      `Evidence identified in ${hit.file_path} via retrieval query '${lastSearch?.query ?? query}'.`,
    );
    setNewMemoryEvidenceFile(hit.file_path);
    setNewMemoryStartLine(hit.start_line);
    setNewMemoryEndLine(hit.end_line);
    setNewMemorySymbol(hit.symbol_name ?? "");
    setPinnedEvidence({
      file_path: hit.file_path,
      start_line: hit.start_line,
      end_line: hit.end_line,
      symbol_name: hit.symbol_name,
      chunk_id: hit.chunk_id,
      chunk_set_id: hit.chunk_set_id,
      config_hash: hit.config_hash,
      materialization_hash: hit.materialization_hash,
      safe_content_sha256: hit.safe_content_sha256,
      sha256: hit.content_sha256,
    });
  };

  const copyCitation = async (citation: string) => {
    try { await navigator.clipboard.writeText(citation); setCopyStatus("Citation copied."); }
    catch { setCopyStatus("Copying is unavailable. Select the citation text to copy it manually."); }
  };

  return (
    <Tabs.Root value={activeTab} onValueChange={value => { if(value === "search" || value === "context" || value === "memory") setActiveTab(value); }} asChild>
    <section aria-labelledby="retrieval-title" style={{ marginTop: 24 }}>
      <div className="section-heading" style={{ marginBottom: 18 }}>
        <div>
          <h2 id="retrieval-title" tabIndex={-1}>Search, context & memory</h2>
          <span className="muted">
            Search published source, assemble bounded context and retain cited
            evidence
          </span>
        </div>
        <Tabs.List aria-label="Source workbench views" className="workbench-tabs">
          <Tabs.Trigger value="search" className={activeTab === "search" ? "primary" : ""}>Code search</Tabs.Trigger>
          <Tabs.Trigger value="context" className={activeTab === "context" ? "primary" : ""}>Context</Tabs.Trigger>
          <Tabs.Trigger value="memory" className={activeTab === "memory" ? "primary" : ""}>Memory</Tabs.Trigger>
        </Tabs.List>
      </div>

      {copyStatus && <InlineNotice>{copyStatus}</InlineNotice>}
      <IndexBuildPanel
        repositoryId={repositoryId}
        snapshotId={snapshotId}
        csrfToken={csrfToken}
      />

      {/* TAB 1: MULTI-MODAL SEARCH */}
      {activeTab === "search" && (
        <Tabs.Content value="search" asChild><div>
          <div
            style={{
              display: "flex",
              gap: 12,
              flexWrap: "wrap",
              alignItems: "center",
              marginBottom: 16,
              padding: 12,
              background: "var(--cs-info-bg)",
              borderRadius: 6,
            }}
          >
            <span
              style={{
                fontWeight: 600,
                fontSize: 13,
                textTransform: "uppercase",
              }}
            >
              Channel:
            </span>
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
            noValidate
            onSubmit={(e) => {
              e.preventDefault();
                if (composingSearch.current) return;
              searchMutation.mutate();
            }}
            style={{
              display: "flex",
              gap: 12,
              maxWidth: "100%",
              flexWrap: "wrap",
              marginBottom: 20,
            }}
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
              ref={searchInput}
              onCompositionStart={() => { composingSearch.current = true; }}
              onCompositionEnd={() => { composingSearch.current = false; }}
              aria-label="Search admitted source"
            />
            {query && (
              <button
                type="button"
                onClick={() => {
                  setQuery("");
                  searchInput.current?.focus();
                }}
              >
                Clear source query
              </button>
            )}
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

          {lastSearch && (
            <div>
              <p className="muted">
                Results for “{lastSearch.query}”. Results set:{" "}
                <code style={{ overflowWrap: "anywhere" }}>
                  {lastSearch.chunk_set_id ?? "unavailable"}
                </code>
              </p>
              {lastSearch.lexical_availability === "index_unavailable" && (
                <p role="status">
                  Source index unavailable. Build a source index before
                  searching.
                </p>
              )}
              {lastSearch.coverage &&
                lastSearch.coverage.processed_files <
                  lastSearch.coverage.admitted_files && (
                  <p role="status">
                    Partial coverage: {lastSearch.coverage.processed_files} of{" "}
                    {lastSearch.coverage.admitted_files} admitted files
                    processed.
                  </p>
                )}
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 12,
                }}
              >
                <span style={{ fontSize: 13, color: "var(--muted)" }}>
                  Retrieved {lastSearch.hits.length} of{" "}
                  {lastSearch.total_candidates} candidates in{" "}
                  {lastSearch.execution_ms}ms
                </span>
                <button
                  type="button"
                  style={{ fontSize: 13, padding: "4px 10px" }}
                  disabled={
                    assembleMutation.isPending || !lastSearch.chunk_set_id
                  }
                  onClick={() => {
                    setActiveTab("context");
                    assembleMutation.mutate();
                  }}
                >
                  Pack Top Hits into Context Packet →
                </button>
              </div>

              {lastSearch.hits.length === 0 ? (
                <p
                  className="muted"
                  style={{
                    padding: 24,
                    textAlign: "center",
                    border: "1px dashed var(--border)",
                  }}
                >
                  {lastSearch.lexical_availability === "index_unavailable"
                    ? "No source index is available for this search."
                    : "No matching source chunks found in this published set."}
                </p>
              ) : (
                <div style={{ display: "grid", gap: 12 }}>
                  {lastSearch.hits.map(
                    (hit: SearchHitV1 | HybridSearchHitV1) => {
                      const isHybrid = "match_modalities" in hit;
                      const hybridHit = isHybrid
                        ? (hit as HybridSearchHitV1)
                        : null;
                      return (
                        <div
                          key={hit.chunk_id}
                          style={{
                            border: "1px solid var(--border)",
                            borderRadius: 6,
                            padding: 14,
                            background: "var(--cs-panel)",
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
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                flexWrap: "wrap",
                              }}
                            >
                              <span
                                style={{
                                  fontWeight: 700,
                                  fontFamily: "var(--cs-mono)",
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
                                    background: "var(--cs-raised)",
                                    padding: "2px 8px",
                                    borderRadius: 4,
                                    fontSize: 12,
                                    fontFamily: "var(--cs-mono)",
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
                                    background:
                                      m === "exact"
                                        ? "var(--cs-success-bg)"
                                        : m === "dense"
                                          ? "var(--cs-info-bg)"
                                          : "var(--cs-info-bg)",
                                    color:
                                      m === "exact"
                                        ? "var(--cs-success)"
                                        : m === "dense"
                                          ? "var(--cs-action)"
                                          : "var(--cs-action)",
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
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 10,
                              }}
                            >
                              <span
                                style={{
                                  fontSize: 12,
                                  fontWeight: 600,
                                  color: "var(--muted)",
                                }}
                              >
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

                          {hybridHit &&
                            Object.keys(hybridHit.modality_ranks).length >
                              0 && (
                              <div
                                style={{
                                  fontSize: 11,
                                  color: "var(--muted)",
                                  marginBottom: 8,
                                }}
                              >
                                Channel Ranks:{" "}
                                {Object.entries(hybridHit.modality_ranks)
                                  .map(([k, r]) => `${k}: #${r}`)
                                  .join(" | ")}
                              </div>
                            )}

                          <pre
                            style={{
                              background: "var(--cs-panel)",
                              color: "var(--cs-text)",
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
                    },
                  )}
                </div>
              )}
            </div>
          )}
        </div></Tabs.Content>
      )}

      {/* TAB 2: CONTEXT ENGINEERING */}
      {activeTab === "context" && (
        <Tabs.Content value="context" asChild><div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: 16,
              marginBottom: 16,
            }}
          >
            <div>
              <label
                style={{ display: "block", marginBottom: 6, fontSize: 13 }}
              >
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
              <label
                style={{ display: "block", marginBottom: 6, fontSize: 13 }}
              >
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
              className="resize-none"
              rows={2}
              value={preamble}
              onChange={(e) => setPreamble(e.target.value)}
              style={{
                minHeight: 80,
                width: "100%",
                padding: 10,
                border: "1px solid var(--border)",
                borderRadius: 4,
                fontFamily: "inherit",
              }}
            />
          </div>

          <label>
            <input
              type="checkbox"
              checked={includeMemory}
              onChange={(event) => setIncludeMemory(event.target.checked)}
            />{" "}
            Include reviewed memory in this preview
          </label>
          <button
            type="button"
            className="primary"
            disabled={assembleMutation.isPending}
            onClick={() => assembleMutation.mutate()}
            style={{ marginBottom: 18 }}
          >
            {assembleMutation.isPending
              ? "Assembling Grounded Context…"
              : "Assemble Grounded Context Packet"}
          </button>

          {lastPacket && (
            <div>
              <div
                style={{
                  background: "var(--cs-raised)",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: 16,
                  marginBottom: 16,
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
                  <span style={{ fontWeight: 700, fontSize: 14 }}>
                    Grounding Packet: {lastPacket.items_count} items packed (
                    {lastPacket.total_tokens} / {maxTokens} tokens)
                  </span>
                  <span
                    style={{
                      fontSize: 12,
                      fontFamily: "var(--cs-mono)",
                      color: "var(--muted)",
                    }}
                  >
                    Digest: {lastPacket.packet_sha256.slice(0, 16)}…
                  </span>
                </div>

                <p>
                  Context for “{lastPacket.query}”. Context set:{" "}
                  <code style={{ overflowWrap: "anywhere" }}>
                    {lastPacket.chunk_set_id ?? "unavailable"}
                  </code>
                </p>
                {/* Token Meter */}
                <div
                  style={{
                    height: 8,
                    background: "var(--cs-border)",
                    borderRadius: 4,
                    overflow: "hidden",
                    marginBottom: 12,
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: `${Math.min(100, (lastPacket.total_tokens / maxTokens) * 100)}%`,
                      background:
                        lastPacket.total_tokens > maxTokens * 0.9
                          ? "var(--cs-danger)"
                          : "var(--action)",
                    }}
                  />
                </div>

                <div style={{ fontSize: 13, color: "var(--muted)" }}>
                  Total Bytes: {lastPacket.total_bytes} bytes | Omitted:{" "}
                  {lastPacket.omitted_count} items
                </div>
              </div>

              <div style={{ display: "grid", gap: 12 }}>
                {preview && (
                  <section aria-label="Memory context selection">
                    <h4>Reviewed memory selection</h4>
                    <p>
                      Selection preview only. System instructions, tools and
                      required history are budgeted again before model dispatch.
                    </p>
                    <p>
                      {preview.memory_tokens} estimated memory tokens ·{" "}
                      {lastPacket.total_tokens} source tokens ·{" "}
                      {lastPacket.total_bytes} source bytes
                    </p>
                    <p>
                      Selection digest: <code>{preview.selection_digest}</code>
                    </p>
                    {preview.memories.items.map((memory) => (
                      <article key={memory.memory_id}>
                        <strong>{memory.topic}</strong> · Version{" "}
                        {memory.version} ·{" "}
                        {memory.historical_hint
                          ? "Historical hint; current-source validation required"
                          : "Current snapshot"}
                        <p>{memory.summary}</p>
                        <p>
                          Source correspondence: {memory.source_correspondence};
                          expiry: {memory.expires_at}
                        </p>
                      </article>
                    ))}
                    {!!preview.memories.omission_reasons?.length && (
                      <p>
                        Omitted: {preview.memories.omission_reasons.join(", ")}
                      </p>
                    )}
                  </section>
                )}
                {lastPacket.items.map((item: ContextItemV1) => (
                  <div
                    key={item.chunk_id}
                    style={{
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: 14,
                      background: "var(--cs-panel)",
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
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                        }}
                      >
                        <button
                          type="button"
                          onClick={() => copyCitation(item.citation_key)}
                          style={{
                            padding: "2px 8px",
                            fontSize: 12,
                            fontWeight: 700,
                            fontFamily: "var(--cs-mono)",
                            background: "var(--cs-info-bg)",
                            color: "var(--cs-action)",
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
                        {item.token_count} tokens | score:{" "}
                        {item.relevance_score.toFixed(4)}
                      </span>
                    </div>

                    <pre
                      style={{
                        background: "var(--cs-panel)",
                        color: "var(--cs-text)",
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
        </div></Tabs.Content>
      )}

      {/* TAB 3: REPOSITORY MEMORY */}
      {activeTab === "memory" && (
        <Tabs.Content value="memory" asChild><div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginBottom: 18 }}>
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
                {recordMemoryMutation.isPending ? "Recording…" : "Submit memory for review"}
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
        </div></Tabs.Content>
      )}

      {/* SOURCE VIEW MODAL */}
      {selectedFileForView && (
        <Modal open title="Source evidence" onClose={() => setSelectedFileForView(null)} variant="drawer">
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
              boxShadow:
                "var(--cs-shadow), 0 10px 10px -5px rgba(0, 0, 0, 0.04)",
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
                      File content omitted:{" "}
                      {sourceViewQuery.data.omission_reason}
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
      )}
    </section></Tabs.Root>
  );
}

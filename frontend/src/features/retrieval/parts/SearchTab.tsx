import * as Tabs from "@radix-ui/react-tabs";
import type { HybridSearchHitV1, SearchHitV1 } from "../types";
import { useRetrieval } from "../retrievalContext";

/** Code search across exact, lexical, dense and hybrid modes. */
export function SearchTab() {
  const {
    searchInputRef,
    composingSearchRef,
    searchMutation,
    assembleMutation,
    handlePinAsMemory,
    setActiveTab,
    searchMode,
    setSearchMode,
    query,
    setQuery,
    limit,
    setLimit,
    denseIndexId,
    setDenseIndexId,
    setSelectedFileForView,
    lastSearch,
  } = useRetrieval();
  return (
    <>
      <Tabs.Content value="search" asChild>
        <div>
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
              if (composingSearchRef.current) return;
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
              ref={searchInputRef}
              onCompositionStart={() => {
                composingSearchRef.current = true;
              }}
              onCompositionEnd={() => {
                composingSearchRef.current = false;
              }}
              aria-label="Search admitted source"
            />
            {query && (
              <button
                type="button"
                onClick={() => {
                  setQuery("");
                  searchInputRef.current?.focus();
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
        </div>
      </Tabs.Content>
    </>
  );
}

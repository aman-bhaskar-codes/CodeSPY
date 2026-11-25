import * as Tabs from "@radix-ui/react-tabs";
import type { ContextItemV1 } from "../types";
import { useRetrieval } from "../retrievalContext";

/** Bounded context assembly for a question. */
export function ContextTab() {
  const {
    assembleMutation,
    copyCitation,
    maxTokens,
    setMaxTokens,
    overheadTokens,
    setOverheadTokens,
    preamble,
    setPreamble,
    includeMemory,
    setIncludeMemory,
    preview,
    lastPacket,
  } = useRetrieval();
  return (
    <>
      <Tabs.Content value="context" asChild>
        <div>
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
        </div>
      </Tabs.Content>
    </>
  );
}

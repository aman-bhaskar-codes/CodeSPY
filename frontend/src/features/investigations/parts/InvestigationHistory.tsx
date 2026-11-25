import { type ExtendedSummary } from "../investigationTypes";
import { useInvestigations } from "../investigationsContext";

/** Past investigations of this repository. */
export function InvestigationHistory() {
  const {
    listQuery,
    formatMicros,
    getVerdictStyle,
    setSelectedInvId,
    selectedInvId,
    historyCursor,
    setHistoryCursor,
  } = useInvestigations();
  return (
    <>
      <div
        style={{
          padding: "16px",
          background: "var(--cs-raised)",
          borderRadius: "8px",
          border: "1px solid var(--cs-border)",
        }}
      >
        <h3
          style={{
            margin: "0 0 12px",
            fontSize: "14px",
            fontWeight: 600,
            color: "var(--cs-text)",
          }}
        >
          Investigations ({listQuery.data?.total_count || 0})
        </h3>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "8px",
            maxHeight: "350px",
            overflowY: "auto",
          }}
        >
          <div className="session-actions">
            {historyCursor && (
              <button type="button" onClick={() => setHistoryCursor(undefined)}>
                Newest investigations
              </button>
            )}
            {listQuery.data &&
              "next_cursor" in listQuery.data &&
              listQuery.data.next_cursor && (
                <button
                  type="button"
                  disabled={listQuery.isFetching}
                  onClick={() =>
                    setHistoryCursor(
                      listQuery.data && "next_cursor" in listQuery.data
                        ? (listQuery.data.next_cursor ?? undefined)
                        : undefined,
                    )
                  }
                >
                  Older investigations
                </button>
              )}
          </div>
          {listQuery.data?.items.map((inv) => (
            <button
              type="button"
              aria-pressed={selectedInvId === inv.id}
              key={inv.id}
              onClick={() => setSelectedInvId(inv.id)}
              style={{
                padding: "10px",
                borderRadius: "6px",
                background:
                  selectedInvId === inv.id
                    ? "var(--cs-raised)"
                    : "var(--cs-panel)",
                border: `1px solid ${selectedInvId === inv.id ? "var(--cs-action)" : "var(--cs-raised)"}`,
                cursor: "pointer",
                display: "flex",
                flexDirection: "column",
                gap: "4px",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: "12px",
                    fontWeight: 600,
                    color: "var(--cs-action)",
                  }}
                >
                  {(inv.query || "").slice(0, 36)}...
                </span>
                <span
                  style={{
                    fontSize: "11px",
                    padding: "2px 6px",
                    borderRadius: "4px",
                    ...getVerdictStyle(inv.verdict),
                  }}
                >
                  {inv.verdict
                    ? inv.verdict.toUpperCase()
                    : (inv.status || "").toUpperCase()}
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: "11px",
                  color: "var(--cs-muted)",
                  flexWrap: "wrap",
                  gap: "4px",
                }}
              >
                <span>
                  Step: {inv.current_step ?? 0}/{inv.max_steps ?? 10}
                </span>
                <span>Spent: {formatMicros(inv.spent_micros ?? 0)}</span>
                {(inv as unknown as ExtendedSummary).held_micros ? (
                  <span style={{ color: "var(--cs-warning)" }}>
                    Held:{" "}
                    {formatMicros(
                      (inv as unknown as ExtendedSummary).held_micros ?? 0,
                    )}
                  </span>
                ) : null}
              </div>
            </button>
          ))}
        </div>
      </div>
    </>
  );
}

import type { InvestigationEvent } from "../types";
import { type EventShape } from "../investigationTypes";
import { useInvestigations } from "../investigationsContext";

/** The live and stored event timeline. */
export function EventTimeline() {
  const { eventsQuery, liveEvents, eventCursor, setEventCursor } =
    useInvestigations();
  return (
    <>
      {(() => {
        const historicalEvents: InvestigationEvent[] = Array.isArray(
          eventsQuery.data,
        )
          ? eventsQuery.data
          : (eventsQuery.data?.items ?? []);

        const allEvents: InvestigationEvent[] = [...historicalEvents];
        for (const ev of eventCursor === undefined ? liveEvents : []) {
          if (
            !allEvents.some(
              (item) =>
                (item.id && item.id === ev.id) ||
                ("sequence" in item &&
                  "sequence" in ev &&
                  item.sequence === ev.sequence),
            )
          ) {
            allEvents.push(ev);
          }
        }

        return (
          <div>
            <h4
              style={{
                margin: "0 0 10px",
                fontSize: "14px",
                fontWeight: 600,
                color: "var(--cs-muted)",
              }}
            >
              Investigation Replay & Audit Trail ({allEvents.length} events)
            </h4>
            <p className="muted">
              This view shows one saved event page and up to 200 recent live
              events. It is not the complete audit history.
            </p>
            <div className="session-actions">
              {eventCursor !== undefined && (
                <button type="button" onClick={() => setEventCursor(undefined)}>
                  First event page
                </button>
              )}
              {eventsQuery.data &&
                !Array.isArray(eventsQuery.data) &&
                eventsQuery.data.next_cursor != null && (
                  <button
                    type="button"
                    disabled={eventsQuery.isFetching}
                    onClick={() =>
                      setEventCursor(
                        eventsQuery.data && !Array.isArray(eventsQuery.data)
                          ? (eventsQuery.data.next_cursor ?? undefined)
                          : undefined,
                      )
                    }
                  >
                    Next event page
                  </button>
                )}
            </div>
            <div
              id="events-timeline"
              style={{
                maxHeight: "180px",
                overflowY: "auto",
                padding: "8px",
                background: "var(--cs-panel)",
                borderRadius: "6px",
                display: "flex",
                flexDirection: "column",
                gap: "6px",
              }}
            >
              {allEvents.length === 0 ? (
                <div
                  style={{
                    fontSize: "12px",
                    color: "var(--cs-muted)",
                    fontStyle: "italic",
                    padding: "4px",
                  }}
                >
                  No replay events recorded yet.
                </div>
              ) : (
                allEvents.map((ev, i) => {
                  const evShape = ev as unknown as EventShape;
                  const evKey =
                    "sequence" in ev && ev.sequence !== undefined
                      ? `seq-${ev.sequence}`
                      : evShape.id || i;
                  const eventType =
                    evShape.event_type || evShape.type || "event";
                  const nodeName = evShape.node_name || evShape.node || "";
                  const seqText =
                    "sequence" in ev && ev.sequence !== undefined
                      ? `#${ev.sequence} `
                      : "";

                  return (
                    <div
                      key={evKey}
                      style={{
                        fontSize: "11px",
                        display: "flex",
                        justifyContent: "space-between",
                        padding: "4px 8px",
                        borderRadius: "4px",
                        background: "var(--cs-raised)",
                      }}
                    >
                      <span
                        style={{
                          color: "var(--cs-action)",
                          fontWeight: 600,
                        }}
                      >
                        {seqText}
                        {eventType}
                      </span>
                      <span style={{ color: "var(--cs-muted)" }}>
                        {nodeName}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        );
      })()}
    </>
  );
}

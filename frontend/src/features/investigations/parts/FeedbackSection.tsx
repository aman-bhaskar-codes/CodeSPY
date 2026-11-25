import { useInvestigations } from "../investigationsContext";

/** Human feedback and saving to verified memory. */
export function FeedbackSection() {
  const {
    feedbackMutation,
    feedbackNotes,
    setFeedbackNotes,
    feedbackType,
    setFeedbackType,
    saveToMemory,
    setSaveToMemory,
  } = useInvestigations();
  return (
    <>
      <div
        style={{
          padding: "16px",
          background: "var(--cs-panel)",
          borderRadius: "8px",
          border: "1px solid var(--cs-border)",
        }}
      >
        <h4
          style={{
            margin: "0 0 10px",
            fontSize: "13px",
            fontWeight: 600,
            color: "var(--cs-text)",
          }}
        >
          Human Review & Repository Memory
        </h4>
        <div
          className="feedback-actions"
          style={{ display: "flex", gap: "8px", marginBottom: "10px" }}
        >
          <button
            id="feedback-agree-btn"
            aria-pressed={feedbackType === "agree"}
            type="button"
            onClick={() => setFeedbackType("agree")}
            style={{
              padding: "6px 12px",
              borderRadius: "4px",
              border: "none",
              background:
                feedbackType === "agree"
                  ? "var(--cs-success-bg)"
                  : "var(--cs-raised)",
              color: "var(--cs-text)",
              fontSize: "12px",
              cursor: "pointer",
            }}
          >
            Confirm (Agree)
          </button>
          <button
            id="feedback-disagree-btn"
            aria-pressed={feedbackType === "disagree"}
            type="button"
            onClick={() => setFeedbackType("disagree")}
            style={{
              padding: "6px 12px",
              borderRadius: "4px",
              border: "none",
              background:
                feedbackType === "disagree"
                  ? "var(--cs-danger-bg)"
                  : "var(--cs-raised)",
              color: "var(--cs-text)",
              fontSize: "12px",
              cursor: "pointer",
            }}
          >
            Reject (Disagree)
          </button>
          <button
            id="feedback-needs-review-btn"
            aria-pressed={feedbackType === "needs_review"}
            type="button"
            onClick={() => setFeedbackType("needs_review")}
            style={{
              padding: "6px 12px",
              borderRadius: "4px",
              border: "none",
              background:
                feedbackType === "needs_review"
                  ? "var(--cs-warning-bg)"
                  : "var(--cs-raised)",
              color: "var(--cs-text)",
              fontSize: "12px",
              cursor: "pointer",
            }}
          >
            Needs More Context
          </button>
        </div>

        <label htmlFor="feedback-notes-input">Review notes</label>
        <textarea
          className="resize-none"
          id="feedback-notes-input"
          value={feedbackNotes}
          onChange={(e) => setFeedbackNotes(e.target.value)}
          placeholder="Add architectural explanation or triage notes for memory persistence..."
          rows={2}
          style={{
            width: "100%",
            padding: "6px 8px",
            borderRadius: "4px",
            border: "1px solid var(--cs-control-border)",
            background: "var(--cs-raised)",
            color: "var(--cs-text)",
            fontSize: "12px",
            boxSizing: "border-box",
            marginBottom: "8px",
          }}
        />

        <div
          className="feedback-actions"
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <label
            style={{
              fontSize: "12px",
              color: "var(--cs-muted)",
              display: "flex",
              alignItems: "center",
              gap: "6px",
            }}
          >
            <input
              id="save-to-memory-checkbox"
              type="checkbox"
              checked={saveToMemory}
              onChange={(e) => setSaveToMemory(e.target.checked)}
            />
            Propose a memory for human review
          </label>

          <button
            id="submit-feedback-btn"
            type="button"
            disabled={feedbackMutation.isPending || !feedbackNotes.trim()}
            onClick={() =>
              feedbackMutation.mutate({
                feedback_type: feedbackType,
                notes: feedbackNotes.trim(),
                save_to_memory: saveToMemory,
              })
            }
            style={{
              padding: "6px 14px",
              borderRadius: "4px",
              border: "none",
              background: "var(--cs-action)",
              color: "var(--cs-action-ink)",
              fontWeight: 600,
              fontSize: "12px",
              cursor:
                !feedbackNotes.trim() || feedbackMutation.isPending
                  ? "not-allowed"
                  : "pointer",
              opacity:
                !feedbackNotes.trim() || feedbackMutation.isPending ? 0.6 : 1,
            }}
          >
            {feedbackMutation.isPending ? "Recording..." : "Save Review"}
          </button>
        </div>
      </div>
    </>
  );
}

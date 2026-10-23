import { useRef, useState } from "react";
import type {
  FindingSummaryV1,
  TriageReason,
  TriageState,
  TriageTransitionV1,
} from "./types";

interface Props {
  finding: FindingSummaryV1;
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (
    transition: TriageTransitionV1,
    requestKey: string,
  ) => Promise<void>;
}

export function TriageModal({ finding, isOpen, onClose, onSubmit }: Props) {
  const [state, setState] = useState<TriageState>(finding.triage_state);
  const [reason, setReason] = useState<TriageReason>(
    finding.triage_reason === "none" &&
      ["false_positive", "mitigated", "accepted_risk"].includes(
        finding.triage_state,
      )
      ? "code_refactor"
      : finding.triage_reason,
  );
  const [isSubmitting, setIsSubmitting] = useState(false);
  const pendingRequest = useRef<{ signature: string; key: string } | null>(
    null,
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  if (!isOpen) return null;

  const requiresReason = [
    "false_positive",
    "mitigated",
    "accepted_risk",
  ].includes(state);
  const isReasonInvalid = requiresReason && reason === "none";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isReasonInvalid) {
      setErrorMessage(
        "A structured justification reason is required for this state.",
      );
      return;
    }

    setIsSubmitting(true);
    setErrorMessage(null);
    try {
      const transition: TriageTransitionV1 = {
        state,
        reason: requiresReason ? reason : "none",
        expected_version: finding.triage_version,
      };
      const signature = JSON.stringify([finding.id, transition]);
      if (pendingRequest.current?.signature !== signature) {
        pendingRequest.current = { signature, key: crypto.randomUUID() };
      }
      await onSubmit(transition, pendingRequest.current.key);
      pendingRequest.current = null;
      onClose();
    } catch (err: unknown) {
      const msg =
        err instanceof Error
          ? err.message
          : "Failed to record triage decision.";
      if (msg.includes("409") || msg.includes("conflict")) {
        setErrorMessage(
          "Conflict: This finding was modified concurrently by another reviewer. Please refresh to view the latest version.",
        );
      } else {
        setErrorMessage(msg);
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="triage-modal-title"
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(15, 23, 42, 0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
        padding: "16px",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget && !isSubmitting) onClose();
      }}
    >
      <div
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "8px",
          maxWidth: "520px",
          width: "100%",
          padding: "24px",
          boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.1)",
        }}
      >
        <h3
          id="triage-modal-title"
          style={{ margin: "0 0 8px", fontSize: "18px", color: "var(--ink)" }}
        >
          Triage Finding
        </h3>
        <p className="muted" style={{ margin: "0 0 16px", fontSize: "13px" }}>
          <code>{finding.rule_id}</code> at{" "}
          <code>
            {finding.file_path}
            {finding.line_number ? `:${finding.line_number}` : ""}
          </code>
        </p>

        {errorMessage && (
          <div
            role="alert"
            style={{
              padding: "10px 14px",
              background: "#fde8e8",
              border: "1px solid var(--danger)",
              borderRadius: "6px",
              color: "var(--danger)",
              fontSize: "13px",
              marginBottom: "16px",
            }}
          >
            {errorMessage}
          </div>
        )}

        <form onSubmit={handleSubmit} style={{ display: "grid", gap: "16px" }}>
          <div>
            <label
              htmlFor="triage-state-select"
              style={{
                display: "block",
                marginBottom: "6px",
                fontSize: "13px",
              }}
            >
              Disposition State *
            </label>
            <select
              id="triage-state-select"
              value={state}
              onChange={(e) => {
                const newState = e.target.value as TriageState;
                setState(newState);
                if (
                  ["false_positive", "mitigated", "accepted_risk"].includes(
                    newState,
                  ) &&
                  reason === "none"
                ) {
                  setReason("code_refactor");
                }
              }}
              style={{ width: "100%" }}
              disabled={isSubmitting}
            >
              <option value="unreviewed">Unreviewed</option>
              <option value="confirmed">Confirmed (Actionable Bug/Flaw)</option>
              <option value="false_positive">False Positive</option>
              <option value="mitigated">
                Mitigated (Compensating Control)
              </option>
              <option value="accepted_risk">Accepted Risk</option>
            </select>
          </div>

          {requiresReason && (
            <div>
              <label
                htmlFor="triage-reason-select"
                style={{
                  display: "block",
                  marginBottom: "6px",
                  fontSize: "13px",
                }}
              >
                Structured Justification Reason *
              </label>
              <select
                id="triage-reason-select"
                value={reason}
                onChange={(e) => setReason(e.target.value as TriageReason)}
                style={{ width: "100%" }}
                disabled={isSubmitting}
              >
                <option value="none" disabled>
                  -- Select justification --
                </option>
                <option value="code_refactor">
                  Code Refactor (Addressed in pending refactor)
                </option>
                <option value="compensating_control">
                  Compensating Control (Protected by upstream WAF / middleware)
                </option>
                <option value="business_context">
                  Business Context (Intended operational design)
                </option>
                <option value="test_code">
                  Test Code (Mock / fixture / harness only)
                </option>
                <option value="vendor_dependency">
                  Vendor Dependency (Upstream library behavior)
                </option>
              </select>
              <span
                className="muted"
                style={{ fontSize: "12px", marginTop: "4px", display: "block" }}
              >
                Structured reasons prevent secret leakage in free-text fields.
              </span>
            </div>
          )}

          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              gap: "10px",
              marginTop: "8px",
            }}
          >
            <button
              type="button"
              onClick={onClose}
              disabled={isSubmitting}
              style={{
                background: "transparent",
                color: "var(--ink)",
                border: "1px solid var(--border)",
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || isReasonInvalid}
              style={{
                background: isReasonInvalid ? "var(--muted)" : "var(--action)",
                color: "#fff",
                border: "none",
                fontWeight: 600,
              }}
            >
              {isSubmitting ? "Recording..." : "Save Triage Decision"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

import type { InvestigationMode } from "../types";

import { useInvestigations } from "../investigationsContext";

/** The form that launches a hypothesis. */
export function NewInvestigationForm() {
  const {
    findingId,
    findingRunId,
    setFindingId,
    setFindingRunId,
    invalidFinding,
    createMutation,
    query,
    setQuery,
    mode,
    setMode,
    maxSteps,
    setMaxSteps,
    budgetMicros,
    setBudgetMicros,
  } = useInvestigations();
  return (
    <>
      <div
        style={{
          padding: "16px",
          background: "var(--cs-raised)",
          borderRadius: "8px",
          border: "1px solid var(--cs-border)",
          display: "flex",
          flexDirection: "column",
          gap: "12px",
        }}
      >
        <h3
          style={{
            margin: 0,
            fontSize: "14px",
            fontWeight: 600,
            color: "var(--cs-text)",
          }}
        >
          New investigation
        </h3>

        <div>
          <label
            htmlFor="investigation-query-input"
            style={{
              display: "block",
              fontSize: "12px",
              color: "var(--cs-muted)",
              marginBottom: "4px",
            }}
          >
            Question or Finding to Investigate:
          </label>
          <textarea
            className="resize-none"
            id="investigation-query-input"
            value={query}
            maxLength={4000}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g. Verify potential hardcoded API key in auth handler or validate SSRF path"
            rows={3}
            style={{
              width: "100%",
              padding: "8px",
              borderRadius: "6px",
              border: "1px solid var(--cs-control-border)",
              background: "var(--cs-panel)",
              color: "var(--cs-text)",
              fontSize: "13px",
              boxSizing: "border-box",
            }}
          />
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns:
              "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
            gap: "12px",
          }}
        >
          <div>
            <label
              htmlFor="investigation-mode-select"
              style={{
                display: "block",
                fontSize: "12px",
                color: "var(--cs-muted)",
                marginBottom: "4px",
              }}
            >
              Orchestration Mode:
            </label>
            <select
              id="investigation-mode-select"
              value={mode}
              onChange={(e) => setMode(e.target.value as InvestigationMode)}
              style={{
                width: "100%",
                padding: "6px 8px",
                borderRadius: "6px",
                border: "1px solid var(--cs-control-border)",
                background: "var(--cs-panel)",
                color: "var(--cs-text)",
                fontSize: "12px",
              }}
            >
              <option value="short_path">Default Short-Path (Direct)</option>
              <option value="two_specialist">
                Two-Specialist (Adversarial)
              </option>
            </select>
          </div>

          <div>
            <label
              htmlFor="investigation-finding-id-input"
              style={{
                display: "block",
                fontSize: "12px",
                color: "var(--cs-muted)",
                marginBottom: "4px",
              }}
            >
              Target Finding ID (Optional):
            </label>
            {findingId.trim() && (
              <>
                <label htmlFor="finding-scan-run">Finding scan run ID</label>
                <input
                  id="finding-scan-run"
                  value={findingRunId}
                  onChange={(event) => setFindingRunId(event.target.value)}
                  aria-invalid={invalidFinding}
                  aria-describedby="finding-binding-hint"
                />
                <p
                  id="finding-binding-hint"
                  className={invalidFinding ? "error" : "muted"}
                >
                  Provide the exact finding and published scan run UUIDs. Use
                  “Investigate this finding” from a finding's detail view to
                  fill them automatically.
                </p>
              </>
            )}
            <input
              id="investigation-finding-id-input"
              type="text"
              value={findingId}
              onChange={(e) => setFindingId(e.target.value)}
              placeholder="UUID (optional)"
              style={{
                width: "100%",
                padding: "6px 8px",
                borderRadius: "6px",
                border: "1px solid var(--cs-control-border)",
                background: "var(--cs-panel)",
                color: "var(--cs-text)",
                fontSize: "12px",
                boxSizing: "border-box",
              }}
            />
          </div>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns:
              "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
            gap: "12px",
          }}
        >
          <div>
            <label
              htmlFor="investigation-budget-select"
              style={{
                display: "block",
                fontSize: "12px",
                color: "var(--cs-muted)",
                marginBottom: "4px",
              }}
            >
              Max Cost Budget:
            </label>
            <select
              id="investigation-budget-select"
              aria-label="Max Cost Budget"
              value={budgetMicros}
              onChange={(e) => setBudgetMicros(Number(e.target.value))}
              style={{
                width: "100%",
                padding: "6px 8px",
                borderRadius: "6px",
                border: "1px solid var(--cs-control-border)",
                background: "var(--cs-panel)",
                color: "var(--cs-text)",
                fontSize: "12px",
              }}
            >
              <option value={250_000}>$0.25 (Test)</option>
              <option value={1_000_000}>$1.00 (Standard)</option>
              <option value={5_000_000}>$5.00</option>
              <option value={10_000_000}>$10.00 (Comprehensive)</option>
            </select>
          </div>

          <div>
            <label
              htmlFor="investigation-max-steps-input"
              style={{
                display: "block",
                fontSize: "12px",
                color: "var(--cs-muted)",
                marginBottom: "4px",
              }}
            >
              Maximum investigation steps:
            </label>
            <input
              id="investigation-max-steps-input"
              aria-label="Maximum investigation steps"
              type="number"
              min={1}
              max={25}
              value={maxSteps}
              onChange={(e) => setMaxSteps(Number(e.target.value))}
              style={{
                width: "100%",
                padding: "6px 8px",
                borderRadius: "6px",
                border: "1px solid var(--cs-control-border)",
                background: "var(--cs-panel)",
                color: "var(--cs-text)",
                fontSize: "12px",
                boxSizing: "border-box",
              }}
            />
          </div>
        </div>

        <button
          id="start-investigation-btn"
          type="button"
          disabled={
            createMutation.isPending ||
            !query.trim() ||
            !Number.isInteger(maxSteps) ||
            maxSteps < 1 ||
            maxSteps > 25 ||
            invalidFinding
          }
          onClick={() => createMutation.mutate()}
          style={{
            marginTop: "8px",
            padding: "8px 16px",
            borderRadius: "6px",
            border: "none",
            background: "var(--cs-action)",
            color: "var(--cs-action-ink)",
            fontWeight: 600,
            fontSize: "13px",
            cursor:
              !query.trim() || createMutation.isPending
                ? "not-allowed"
                : "pointer",
            opacity: !query.trim() || createMutation.isPending ? 0.6 : 1,
          }}
        >
          {createMutation.isPending
            ? "Initializing..."
            : "Create investigation"}
        </button>
        <p className="muted">
          Creation saves the request. Execute starts budgeted investigation
          work.
        </p>
      </div>
    </>
  );
}

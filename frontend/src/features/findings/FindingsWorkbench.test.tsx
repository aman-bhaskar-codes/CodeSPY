import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FindingsSummaryCard } from "./FindingsSummaryCard";
import { TriageModal } from "./TriageModal";
import type { FindingSummaryV1, ScanSummaryResponseV1 } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("FindingsSummaryCard", () => {
  it("never renders a false-safe badge when 0 findings are present", () => {
    const zeroSummary: ScanSummaryResponseV1 = {
      schema_version: 1,
      run_id: "00000000-0000-0000-0000-000000000001",
      snapshot_id: "00000000-0000-0000-0000-000000000002",
      status: "succeeded",
      created_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
      counts_by_severity: { critical: 0, high: 0, medium: 0, low: 0, info: 0 },
      counts_by_category: { sast: 0, secrets: 0, sca: 0, iac: 0 },
      counts_by_disposition: { proposed: 0, "supported-static": 0, contradicted: 0, unresolved: 0 },
      counts_by_triage_state: { unreviewed: 0, confirmed: 0, false_positive: 0, mitigated: 0, accepted_risk: 0 },
      total_findings: 0,
    };

    render(<FindingsSummaryCard summary={zeroSummary} />);

    // Zero false-safe badge mandate: Never show green "Safe" or "Vulnerability-Free"
    expect(screen.queryByText(/safe/i)).toBeNull();
    expect(screen.queryByText(/vulnerability-free/i)).toBeNull();

    // Must show clear neutral disclaimer
    expect(
      screen.getByText(/0 findings detected across scanned rules/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/does not indicate absence of all vulnerabilities/i),
    ).toBeInTheDocument();
  });

  it("renders accurate counts by severity", () => {
    const summary: ScanSummaryResponseV1 = {
      schema_version: 1,
      run_id: "00000000-0000-0000-0000-000000000001",
      snapshot_id: "00000000-0000-0000-0000-000000000002",
      status: "succeeded",
      created_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
      counts_by_severity: { critical: 2, high: 5, medium: 8, low: 1, info: 0 },
      counts_by_category: { sast: 10, secrets: 4, sca: 2, iac: 0 },
      counts_by_disposition: { proposed: 6, "supported-static": 10, contradicted: 0, unresolved: 0 },
      counts_by_triage_state: { unreviewed: 14, confirmed: 2, false_positive: 0, mitigated: 0, accepted_risk: 0 },
      total_findings: 16,
    };

    render(<FindingsSummaryCard summary={summary} />);

    expect(screen.getByText("16 total findings")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument(); // critical
    expect(screen.getByText("5")).toBeInTheDocument(); // high
    expect(screen.getByText("8")).toBeInTheDocument(); // medium
  });
});

describe("TriageModal", () => {
  const dummyFinding: FindingSummaryV1 = {
    id: "00000000-0000-0000-0000-000000000010",
    rule_id: "CS-SEC-001",
    category: "secrets",
    severity: "critical",
    disposition: "supported-static",
    file_path: "src/config.py",
    line_number: 42,
    message: "Hardcoded API secret token detected",
    triage_state: "unreviewed",
    triage_reason: "none",
    triage_updated_at: null,
    triage_version: 1,
  };

  it("renders dialog with finding information and prevents free-text entry", () => {
    render(
      <TriageModal
        finding={dummyFinding}
        isOpen={true}
        onClose={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText("Triage Finding")).toBeInTheDocument();
    expect(screen.getByText("CS-SEC-001")).toBeInTheDocument();
    expect(screen.getByText(/src\/config\.py/)).toBeInTheDocument();

    // Ensure NO free-text comments or textarea elements exist to prevent credential leak
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

import "@testing-library/jest-dom/vitest";
import userEvent from "@testing-library/user-event";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
afterEach(cleanup);
import type {
  FeatureTraceV1,
  FindingDetailResponseV1,
  ScanSummaryResponseV1,
} from "../../api/generated/types.gen";
import { FindingsSummaryCard } from "./FindingsSummaryCard";
import { FindingDetailDrawer } from "./FindingDetailDrawer";
import { FeatureTraceGraph } from "./FeatureTraceGraph";

it("renders severity totals from the backend summary contract", () => {
  const summary: ScanSummaryResponseV1 = {
    scan_id: "run",
    repository_id: "repo",
    snapshot_id: "snapshot",
    status: "completed",
    total_findings: 5,
    by_category: { source_pattern: 5 },
    by_severity: { high: 5 },
    by_disposition: { unresolved: 5 },
    by_triage: { unreviewed: 5 },
    created_at: null,
  };
  const html = renderToString(<FindingsSummaryCard summary={summary} />);
  expect(html).toMatch(/5(?:<!-- -->)?\s*total findings/);
  expect(html).toMatch(/>5<\/div>/);
  expect(html).not.toContain("In progress");
});

it("renders flat finding detail and resets cancelled triage to the current version", async () => {
  const detail: FindingDetailResponseV1 = {
    id: "finding",
    repository_id: "repo",
    snapshot_id: "snapshot",
    run_id: "run",
    category: "source_pattern",
    rule_id: "fixture.rule",
    severity: "high",
    title: "Fixture finding",
    message: "Review the call configuration",
    file_path: "src/example.py",
    line_number: 10,
    end_line: 10,
    code_excerpt: "10: run(command)",
    disposition: "unresolved",
    historical_disposition: "supported-static",
    support_reason: "source_unavailable",
    triage: {
      finding_id: "finding",
      current_state: "unreviewed",
      current_reason: "none",
      version: 0,
    },
    evidence_ids: ["evidence-123456789"],
    limitations: ["Static candidate only"],
  };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(["finding", "repo", "snapshot", "finding"], detail);
  client.setQueryData(["finding-history", "repo", "snapshot", "finding"], {
    events: [],
  });
  const rendered = render(
    <QueryClientProvider client={client}>
      <FindingDetailDrawer
        repositoryId="repo"
        snapshotId="snapshot"
        findingId="finding"
        csrfToken="synthetic"
        isOpen
        onClose={() => {}}
        onTriageUpdated={() => {}}
      />
    </QueryClientProvider>,
  );
  const html = rendered.baseElement.innerHTML;
  expect(html).toContain("Review the call configuration");
  expect(html).toContain("10: run(command)");
  expect(html).toContain("source_unavailable");
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Change Triage State" }));
  await user.selectOptions(
    screen.getByRole("combobox", { name: /Disposition State/ }),
    "mitigated",
  );
  await user.keyboard("{Escape}");
  expect(
    screen.getByRole("button", { name: "Change Triage State" }),
  ).toHaveFocus();
  act(() =>
    client.setQueryData(["finding", "repo", "snapshot", "finding"], {
      ...detail,
      triage: {
        ...detail.triage,
        current_state: "accepted_risk",
        current_reason: "business_requirement",
        version: 2,
      },
    }),
  );
  await user.click(screen.getByRole("button", { name: "Change Triage State" }));
  expect(
    screen.getByRole("combobox", { name: /Disposition State/ }),
  ).toHaveValue("accepted_risk");
  client.clear();
});

it("renders the backend trace without invented route endpoint fields", () => {
  const trace: FeatureTraceV1 = {
    finding_id: "finding",
    nodes: [
      {
        id: "finding",
        kind: "finding",
        label: "Fixture finding",
        file_path: "src/example.py",
        line: 10,
        properties: {},
      },
    ],
    edges: [],
    hop_count: 0,
    budget_exceeded: true,
    structural_run_id: "structural-run",
    omission_reasons: ["depth"],
  };
  const html = renderToString(<FeatureTraceGraph trace={trace} />);
  expect(html).toContain("Fixture finding");
  expect(html).toContain("Budget");
  expect(html).toContain("Trace coverage:");
  expect(html).toContain("structural-run");
  expect(html).toMatch(/src\/example.py(?:<!-- -->)?:10/);
});

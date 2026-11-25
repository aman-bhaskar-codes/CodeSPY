import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../api/client";
import type {
  ArchitectureGraphPageV1,
  ArchitectureReportRefV1,
  ArchitectureShardsV1,
  RepositoryReportV1,
} from "../../api/generated/types.gen";
import { ArchitectureWorkbench } from "./ArchitectureWorkbench";
import { statementText } from "./statementText";

const repo = "10000000-0000-4000-8000-000000000001";
const snapshot = "10000000-0000-4000-8000-000000000002";
const reportId = "10000000-0000-4000-8000-000000000003";
const digest = "a".repeat(64);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function shards(state: string, ready: boolean): ArchitectureShardsV1 {
  return {
    plan_digest: digest,
    total_files: 12,
    skipped_files: 0,
    ready,
    shards: [
      {
        index: 0,
        files: 12,
        prefixes: 0,
        state: state as "queued",
        job_id: state === "not_started" ? null : repo,
        run_id: ready ? snapshot : null,
      },
    ],
  };
}

const ref: ArchitectureReportRefV1 = {
  id: reportId,
  repository_id: repo,
  snapshot_id: snapshot,
  inputs_digest: digest,
  report_digest: digest,
  size_bytes: 100,
  complete: false,
  analyzed_files: 10,
  eligible_files: 12,
};

function report(): RepositoryReportV1 {
  const range = {
    start_byte: 0,
    end_byte: 5,
    start_line: 7,
    end_line: 7,
    start_col: 0,
    end_col: 5,
  };
  return {
    schema_version: 1,
    org_id: repo,
    repository_id: repo,
    snapshot_id: snapshot,
    inputs_digest: digest,
    coverage: {
      eligible_files: 12,
      analyzed_files: 10,
      partial_files: 1,
      omitted_files: 2,
      fact_runs: [],
    },
    graph: { nodes: [], edges: [] },
    sections: [
      {
        name: "coverage",
        statements: [
          {
            kind: "unknown",
            code: "limitation.manifests_names_only",
            params: {},
            evidence: [],
            evidence_total: 0,
          },
        ],
        tables: [],
      },
      {
        name: "overview",
        statements: [
          {
            kind: "derivation",
            code: "overview.files",
            params: { analyzed: 10 },
            evidence: [],
            evidence_total: 0,
          },
          {
            kind: "inference",
            code: "stack.framework",
            params: { framework: "Flask", files: 4 },
            evidence: [
              {
                file_id: repo,
                path: "app/web.py",
                fact: "import",
                range,
              },
            ],
            evidence_total: 5,
          },
        ],
        tables: [
          {
            name: "languages",
            columns: ["language", "files", "symbols"],
            rows: [["python", 10, 40]],
            rows_total: 3,
          },
        ],
      },
      {
        name: "components",
        statements: [],
        tables: [
          {
            name: "components",
            columns: [
              "component",
              "files",
              "languages",
              "inbound",
              "outbound",
              "layer",
            ],
            rows: [
              ["app", 6, "python:6", 0, 3, 0],
              ["lib", 4, "python:4", 3, 0, 1],
            ],
            rows_total: 2,
          },
        ],
      },
    ],
  };
}

const graph: ArchitectureGraphPageV1 = {
  nodes: [
    {
      id: "file:app/a.py",
      label: "app/a.py",
      kind: "file",
      language: "python",
      group: "app",
    },
    {
      id: "file:lib/b.py",
      label: "lib/b.py",
      kind: "file",
      language: "python",
      group: "lib",
    },
  ],
  links: [
    {
      source: "file:app/a.py",
      target: "file:lib/b.py",
      resolution: "resolved",
    },
  ],
  nodes_total: 2,
  links_total: 1,
  offset: 0,
  ambiguous: 0,
  unresolved: 0,
};

const templates = {
  templates: {
    "overview.files": "{analyzed} source files were analyzed.",
    "stack.framework":
      "{framework} appears to be used (imported by {files} files).",
    "limitation.manifests_names_only": "Package manifests are not parsed.",
  },
};

function mount(options: {
  status: ArchitectureShardsV1;
  latest: ArchitectureReportRefV1 | null;
}) {
  vi.spyOn(api, "architectureStatus").mockResolvedValue(options.status);
  vi.spyOn(api, "architectureLatest").mockResolvedValue(options.latest);
  vi.spyOn(api, "architectureTemplates").mockResolvedValue(templates);
  vi.spyOn(api, "architectureReport").mockResolvedValue(report());
  vi.spyOn(api, "architectureGraph").mockResolvedValue(graph);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ArchitectureWorkbench
        repositoryId={repo}
        snapshotId={snapshot}
        csrfToken="csrf-token"
      />
    </QueryClientProvider>,
  );
}

describe("ArchitectureWorkbench", () => {
  it("prepares the analysis and keeps building disabled until every shard is published", async () => {
    const prepare = vi
      .spyOn(api, "architecturePrepare")
      .mockResolvedValue(shards("queued", false));
    mount({ status: shards("not_started", false), latest: null });
    expect(
      await screen.findByText(/12 analyzable files in 1 shard; not started/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build report" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Prepare analysis" }));
    await waitFor(() =>
      expect(prepare).toHaveBeenCalledWith(repo, snapshot, "csrf-token"),
    );
    expect(await screen.findByText(/0 published/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Continue analysis" }),
    ).toBeInTheDocument();
  });

  it("builds once with a stable key and explains a pending refusal", async () => {
    const build = vi
      .spyOn(api, "architectureBuild")
      .mockRejectedValue(new ApiError(409, "report_inputs_pending"));
    mount({ status: shards("succeeded", true), latest: null });
    const button = await screen.findByRole("button", { name: "Build report" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(build).toHaveBeenCalledTimes(1));
    expect(build.mock.calls[0]?.[2]).toBe("csrf-token");
    expect(await screen.findByRole("alert")).toHaveTextContent(/still running/);
  });

  it("shows typed statements, evidence, tables and a loud partial-coverage banner", async () => {
    mount({ status: shards("succeeded", true), latest: ref });
    const overview = await screen.findByRole("region", { name: "Overview" });
    const items = within(overview).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Derived");
    expect(items[0]).toHaveTextContent("10 source files were analyzed.");
    expect(items[1]).toHaveTextContent("Inferred");
    expect(items[1]).toHaveTextContent(
      "Flask appears to be used (imported by 4 files).",
    );
    expect(items[1]).toHaveTextContent("app/web.py:7");
    expect(items[1]).toHaveTextContent("and 2 more");
    expect(
      within(overview).getByRole("table", { name: "languages" }),
    ).toHaveTextContent("python");
    expect(
      within(overview).getByText("2 more rows not shown."),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Partial coverage: 10 of 12 eligible files",
    );
    const coverage = screen.getByRole("region", {
      name: "Coverage and limitations",
    });
    expect(coverage).toHaveTextContent("Unknown");
    expect(coverage).toHaveTextContent("Package manifests are not parsed.");
    expect(
      screen.getByRole("link", { name: "Export Markdown" }),
    ).toHaveAttribute(
      "href",
      expect.stringContaining(`/architecture-reports/${reportId}/markdown`),
    );
    const sections = screen
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(sections.indexOf("Overview")).toBeLessThan(
      sections.indexOf("Coverage and limitations"),
    );
    const drawing = await screen.findByRole("img", {
      name: "Component dependency graph",
    });
    expect(drawing).toBeInTheDocument();
  });
});

describe("statementText", () => {
  it("fills parameters, marks missing ones and falls back for unknown codes", () => {
    const base = {
      kind: "derivation" as const,
      params: { a: 1 },
      evidence: [],
    };
    expect(statementText({ x: "n={a} m={b}" }, { ...base, code: "x" })).toBe(
      "n=1 m=?",
    );
    expect(statementText({}, { ...base, code: "nope.code" })).toBe(
      "nope.code (a=1)",
    );
    expect(
      statementText(undefined, { ...base, code: "plain", params: {} }),
    ).toBe("plain");
  });

  it("checks architecture rules for the shown report with suggestions from its graph", async () => {
    const check = vi.spyOn(api, "architectureRuleCheck").mockResolvedValue({
      schema_version: 1,
      report_id: ref.id,
      inputs_digest: "x",
      results: [
        {
          name: "no-lib",
          kind: "forbidden",
          status: "passed",
          violations_total: 0,
          files_in_scope: 2,
          unknown_imports: 0,
          violations: [],
        },
      ],
    } as never);
    mount({ status: shards("succeeded", true), latest: ref });
    const rules = await screen.findByRole("region", {
      name: "Architecture rules",
    });
    const group = within(rules).getByLabelText(/Group 1 path prefixes/);
    const options = [
      ...document
        .getElementById(group.getAttribute("list")!)!
        .querySelectorAll("option"),
    ].map((o) => o.getAttribute("value"));
    expect(options).toContain("lib");
    fireEvent.change(within(rules).getByLabelText("Rule name"), {
      target: { value: "no-lib" },
    });
    fireEvent.change(group, { target: { value: "app" } });
    fireEvent.change(within(rules).getByLabelText(/Group 2 path prefixes/), {
      target: { value: "lib" },
    });
    fireEvent.click(within(rules).getByRole("button", { name: "Add rule" }));
    fireEvent.click(within(rules).getByRole("button", { name: "Check rules" }));
    await within(rules).findByText("Passed");
    expect(check.mock.calls[0]?.slice(0, 3)).toEqual([repo, snapshot, ref.id]);
    expect(check.mock.calls[0]?.[4]).toBe("csrf-token");
  });
});

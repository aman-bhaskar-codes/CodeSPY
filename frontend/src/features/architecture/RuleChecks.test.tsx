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
import type { RuleCheckV1 } from "../../api/generated/types.gen";
import { RuleChecks } from "./RuleChecks";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const result = (over: Partial<RuleCheckV1> = {}): RuleCheckV1 =>
  ({
    schema_version: 1,
    report_id: "rep",
    inputs_digest: "d1",
    results: [
      {
        name: "no-ui-data",
        kind: "forbidden",
        status: "violated",
        violations_total: 3,
        files_in_scope: 5,
        unknown_imports: 0,
        violations: [
          {
            source: "ui/page.py",
            target: "data/repo.py",
            chain: ["ui/page.py", "service/logic.py", "data/repo.py"],
            evidence: {
              file_id: "f",
              path: "ui/page.py",
              fact: "import",
              range: { start_line: 7 },
            },
          },
        ],
      },
      {
        name: "layers",
        kind: "layers",
        status: "passed",
        violations_total: 0,
        files_in_scope: 9,
        unknown_imports: 0,
        violations: [],
      },
      {
        name: "tools-isolated",
        kind: "independence",
        status: "unknown",
        violations_total: 0,
        files_in_scope: 2,
        unknown_imports: 4,
        violations: [],
      },
    ],
    ...over,
  }) as RuleCheckV1;

function mount(props: Partial<React.ComponentProps<typeof RuleChecks>> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RuleChecks
        repositoryId="r"
        snapshotId="s"
        reportId="rep"
        inputsDigest="d1"
        csrfToken="csrf"
        suggestions={["ui", "data"]}
        {...props}
      />
    </QueryClientProvider>,
  );
}

function addRule(name: string, g1: string, g2: string, kind?: string) {
  fireEvent.change(screen.getByLabelText("Rule name"), {
    target: { value: name },
  });
  if (kind)
    fireEvent.change(screen.getByLabelText("Kind"), {
      target: { value: kind },
    });
  fireEvent.change(screen.getByLabelText(/Group 1 path prefixes/), {
    target: { value: g1 },
  });
  fireEvent.change(screen.getByLabelText(/Group 2 path prefixes/), {
    target: { value: g2 },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
}

describe("RuleChecks", () => {
  it("adds a rule, runs the check with the csrf token and shows each status", async () => {
    const spy = vi
      .spyOn(api, "architectureRuleCheck")
      .mockResolvedValue(result());
    mount();
    expect(screen.getByRole("button", { name: "Check rules" })).toBeDisabled();
    addRule("no-ui-data", "ui/, web", "data");
    expect(
      screen.getByRole("list", { name: "Rules to check" }),
    ).toHaveTextContent("no-ui-data");
    fireEvent.click(screen.getByRole("button", { name: "Check rules" }));
    await screen.findByRole("list", { name: "Rule results" });
    expect(spy).toHaveBeenCalledWith(
      "r",
      "s",
      "rep",
      {
        rules: [
          {
            name: "no-ui-data",
            kind: "forbidden",
            groups: [["ui", "web"], ["data"]],
          },
        ],
      },
      "csrf",
    );
    const results = screen.getByRole("list", { name: "Rule results" });
    expect(within(results).getByText("Violated")).toBeInTheDocument();
    expect(within(results).getByText("Passed")).toBeInTheDocument();
    expect(within(results).getByText("Unknown")).toBeInTheDocument();
    expect(results).toHaveTextContent(
      "4 imports from these files could not be located",
    );
  });

  it("shows the import chain, evidence location and the truncation line", async () => {
    vi.spyOn(api, "architectureRuleCheck").mockResolvedValue(result());
    mount();
    addRule("no-ui-data", "ui", "data");
    fireEvent.click(screen.getByRole("button", { name: "Check rules" }));
    const violations = await screen.findByRole("list", {
      name: "Violations of no-ui-data",
    });
    expect(violations).toHaveTextContent(
      "ui/page.py imports data/repo.py via ui/page.py → service/logic.py → data/repo.py",
    );
    expect(violations).toHaveTextContent("ui/page.py:7");
    expect(violations).toHaveTextContent("2 more violations not shown.");
  });

  it("validates before adding and explains the problem", () => {
    mount();
    addRule("bad name!", "ui", "data");
    expect(screen.getByRole("alert")).toHaveTextContent(/Name/);
    addRule("ok", "../etc", "data");
    expect(screen.getByRole("alert")).toHaveTextContent(
      /not a valid path prefix/,
    );
    expect(screen.getByLabelText("Rule name")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(
      screen.queryByRole("list", { name: "Rules to check" }),
    ).not.toBeInTheDocument();
  });

  it("rejects duplicate names and removes rules", () => {
    mount();
    addRule("one", "ui", "data");
    addRule("one", "a", "b");
    expect(screen.getByRole("alert")).toHaveTextContent("unique");
    fireEvent.click(screen.getByRole("button", { name: "Remove rule one" }));
    expect(
      screen.queryByRole("list", { name: "Rules to check" }),
    ).not.toBeInTheDocument();
  });

  it("offers per-kind groups and drops allow_indirect for layers", async () => {
    const spy = vi
      .spyOn(api, "architectureRuleCheck")
      .mockResolvedValue(result({ results: [] }));
    mount();
    fireEvent.change(screen.getByLabelText("Kind"), {
      target: { value: "layers" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add group" }));
    expect(screen.getByLabelText(/Group 3 path prefixes/)).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/Direct imports only/),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Rule name"), {
      target: { value: "stack" },
    });
    fireEvent.change(screen.getByLabelText(/Group 1 path prefixes/), {
      target: { value: "ui" },
    });
    fireEvent.change(screen.getByLabelText(/Group 2 path prefixes/), {
      target: { value: "service" },
    });
    fireEvent.change(screen.getByLabelText(/Group 3 path prefixes/), {
      target: { value: "data" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    fireEvent.click(screen.getByRole("button", { name: "Check rules" }));
    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(spy.mock.calls[0]?.[3]).toEqual({
      rules: [
        {
          name: "stack",
          kind: "layers",
          groups: [["ui"], ["service"], ["data"]],
        },
      ],
    });
  });

  it("sends allow_indirect only when chosen for forbidden rules", async () => {
    const spy = vi
      .spyOn(api, "architectureRuleCheck")
      .mockResolvedValue(result({ results: [] }));
    mount();
    fireEvent.click(screen.getByLabelText(/Direct imports only/));
    addRule("direct", "ui", "data");
    fireEvent.click(screen.getByRole("button", { name: "Check rules" }));
    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(spy.mock.calls[0]?.[3].rules[0]).toMatchObject({
      allow_indirect: true,
    });
  });

  it("reports server rejection and flags results for a different report", async () => {
    vi.spyOn(api, "architectureRuleCheck").mockRejectedValueOnce(
      new ApiError(422, "invalid"),
    );
    mount();
    addRule("x", "ui", "data");
    fireEvent.click(screen.getByRole("button", { name: "Check rules" }));
    expect(
      await screen.findByText("The server rejected these rules."),
    ).toBeInTheDocument();
    cleanup();
    vi.spyOn(api, "architectureRuleCheck").mockResolvedValue(
      result({ inputs_digest: "other" }),
    );
    mount();
    addRule("x", "ui", "data");
    fireEvent.click(screen.getByRole("button", { name: "Check rules" }));
    expect(
      await screen.findByText(/different report than the one shown/),
    ).toBeInTheDocument();
  });

  it("shows the suggestion note and uses the suggestions list", () => {
    mount({
      suggestionsNote: "Suggestions are based on the first 5000 of 6000 files.",
    });
    expect(screen.getByText(/first 5000 of 6000/)).toBeInTheDocument();
    const input = screen.getByLabelText(/Group 1 path prefixes/);
    const list = document.getElementById(input.getAttribute("list")!)!;
    expect(
      [...list.querySelectorAll("option")].map((o) => o.getAttribute("value")),
    ).toEqual(["ui", "data"]);
  });
});

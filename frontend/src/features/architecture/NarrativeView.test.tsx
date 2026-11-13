import "@testing-library/jest-dom/vitest";
import {
  act,
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
import type { ArchitectureNarrativeV1 } from "../../api/generated/types.gen";
import { NarrativeView } from "./NarrativeView";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const narrative = (
  over: Partial<ArchitectureNarrativeV1["narrative"]> = {},
): ArchitectureNarrativeV1 =>
  ({
    schema_version: 1,
    architecture_report_id: "rep",
    investigation_id: "inv",
    investigation_report_id: "ir",
    created_at: "2026-11-13T00:00:00Z",
    narrative: {
      sentences: [
        {
          claim_id: "u0-c0",
          text: "The code has 12 resolved imports.",
          sids: ["a".repeat(16)],
        },
        {
          claim_id: "u0-c1",
          text: "There are 99 files.",
          sids: ["a".repeat(16)],
        },
        {
          claim_id: "u0-c2",
          text: "3 imports are unknown.",
          sids: ["b".repeat(16)],
        },
      ],
      verifications: [
        { claim_id: "u0-c0", status: "confirmed", basis: "derived" },
        {
          claim_id: "u0-c1",
          status: "unsupported",
          reason: "the number 99 does not appear in the cited statements",
        },
        { claim_id: "u0-c2", status: "confirmed", basis: "unknown" },
      ],
      confirmed: 2,
      total: 3,
      grounded: false,
      unnarrated_sections: ["coverage"],
      unparsed_units: 1,
      dropped_sentences: 0,
      prompt_revision: "report_narrative.v1.x",
      report_digest: "d".repeat(64),
      ...over,
    },
    cited: [
      {
        sid: "a".repeat(16),
        section: "dependencies",
        kind: "derivation",
        code: "dependencies.counts",
        text: "Imports: 12 resolved.",
        evidence: ["src/a.py:3"],
      },
      {
        sid: "b".repeat(16),
        section: "dependencies",
        kind: "unknown",
        code: "dependencies.unresolved",
        text: "3 imports could not be located.",
        evidence: [],
      },
    ],
  }) as ArchitectureNarrativeV1;

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NarrativeView
        repositoryId="r"
        snapshotId="s"
        reportId="rep"
        csrfToken="csrf"
      />
    </QueryClientProvider>,
  );
}

describe("NarrativeView", () => {
  it("shows each sentence with its basis, marks unverified ones and explains why", async () => {
    vi.spyOn(api, "architectureNarrative").mockResolvedValue(narrative());
    mount();
    const list = await screen.findByRole("list", {
      name: "Narrative sentences",
    });
    const items = within(list)
      .getAllByRole("listitem")
      .filter((li) => li.parentElement === list);
    expect(items[0]).toHaveTextContent("Derived");
    expect(items[0]).toHaveTextContent("The code has 12 resolved imports.");
    expect(items[1]).toHaveTextContent("Unverified");
    expect(items[1]).toHaveTextContent("the number 99 does not appear");
    expect(items[2]).toHaveTextContent("Unknown");
    expect(
      screen.getByText(/2 of 3 sentences confirmed.*not verified overall/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Not narrated: coverage\./)).toBeInTheDocument();
    expect(
      screen.getByText(/1 model replies could not be read/),
    ).toBeInTheDocument();
  });

  it("expands the cited statements with their evidence locations", async () => {
    vi.spyOn(api, "architectureNarrative").mockResolvedValue(narrative());
    mount();
    const summaries = await screen.findAllByText(/Cited statements \(1\)/);
    fireEvent.click(summaries[0]!);
    expect(
      screen.getAllByText(/Imports: 12 resolved\./).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText("src/a.py:3").length).toBeGreaterThan(0);
  });

  it("starts a narrative with the csrf token and waits for it without hammering", async () => {
    const read = vi.spyOn(api, "architectureNarrative").mockResolvedValue(null);
    const start = vi
      .spyOn(api, "architectureNarrativeStart")
      .mockResolvedValue("inv");
    mount();
    fireEvent.click(
      await screen.findByRole("button", { name: "Write narrative" }),
    );
    await waitFor(() =>
      expect(start).toHaveBeenCalledWith("r", "s", "rep", "csrf"),
    );
    expect(await screen.findByText(/Narrative requested/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Write narrative" }),
    ).toBeDisabled();
    expect(read).toHaveBeenCalled();
  });

  it("says plainly when no model runtime is configured", async () => {
    vi.spyOn(api, "architectureNarrative").mockResolvedValue(null);
    vi.spyOn(api, "architectureNarrativeStart").mockRejectedValue(
      new ApiError(503, "inference_unavailable"),
    );
    mount();
    fireEvent.click(
      await screen.findByRole("button", { name: "Write narrative" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /no qualified model runtime/,
    );
  });

  it("stops waiting after ten minutes and says so", async () => {
    vi.spyOn(api, "architectureNarrative").mockResolvedValue(null);
    vi.spyOn(api, "architectureNarrativeStart").mockResolvedValue("inv");
    mount();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(
      await screen.findByRole("button", { name: "Write narrative" }),
    );
    await screen.findByText(/Narrative requested/);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 1000);
    });
    expect(
      await screen.findByText(/No narrative has been published yet/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Write narrative" }),
    ).toBeEnabled();
  });
});

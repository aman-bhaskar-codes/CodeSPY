import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import type {
  InvestigationDetailResponseV1,
  InvestigationSummaryV1,
} from "../../api/generated/types.gen";
import { InvestigationsWorkbench } from "./InvestigationsWorkbench";

const repo = "10000000-0000-4000-8000-000000000001",
  snapshot = "10000000-0000-4000-8000-000000000002",
  first = "10000000-0000-4000-8000-000000000003",
  second = "10000000-0000-4000-8000-000000000004";
const streams: { onmessage?: (event: MessageEvent) => void }[] = [];
function summary(id: string, query: string): InvestigationSummaryV1 {
  return {
    id,
    query,
    repository_id: repo,
    org_id: repo,
    status: "pending",
    depth: "standard",
    fencing_token: 1,
    mode: "short_path",
    target_type: "repository",
    current_step: 0,
    max_steps: 10,
    budget_limit_micros: 1000000,
    spent_micros: 0,
    created_at: "2026-10-28T00:00:00Z",
    updated_at: "2026-10-28T00:00:00Z",
  };
}
function mount() {
  window.history.replaceState(
    {},
    "",
    `/?repository=${repo}&snapshot=${snapshot}&investigation=${first}#workbench-title`,
  );
  vi.spyOn(api, "modelAvailability").mockResolvedValue({ profiles: [] });
  vi.spyOn(api, "listInvestigations").mockResolvedValue({
    items: [summary(first, "Question A"), summary(second, "Question B")],
    total_count: 2,
  });
  vi.spyOn(api, "getInvestigation").mockImplementation(async (_repo, id) => ({
    summary: summary(id, id === first ? "Question A" : "Question B"),
    claims: [],
  }));
  vi.spyOn(api, "listInvestigationEvents").mockResolvedValue({ items: [] });
  vi.stubGlobal(
    "EventSource",
    class {
      onmessage?: (event: MessageEvent) => void;
      constructor() {
        streams.push(this);
      }
      addEventListener() {}
      close() {}
    },
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <InvestigationsWorkbench
        repositoryId={repo}
        snapshotId={snapshot}
        csrfToken="fixture"
      />
    </QueryClientProvider>,
  );
  return client;
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  streams.length = 0;
  window.history.replaceState({}, "", "/");
});

it("clears live events, feedback and cancellation intent on history restoration", async () => {
  mount();
  await screen.findByRole("heading", { name: "Question A" });
  act(() =>
    streams
      .at(-1)
      ?.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            id: "a-event",
            event_type: "step_started",
            node_name: "Old node",
            created_at: "2026-10-28T00:00:00Z",
          }),
        }),
      ),
  );
  expect(screen.getByText("Old node")).toBeVisible();
  fireEvent.change(
    screen.getByPlaceholderText(/Add architectural explanation/i),
    { target: { value: "Draft about A" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(
    screen.getByRole("dialog", { name: "Cancel investigation" }),
  ).toBeVisible();
  act(() => {
    window.history.replaceState(
      {},
      "",
      `/?repository=${repo}&snapshot=${snapshot}&investigation=${second}#workbench-title`,
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await screen.findByRole("heading", { name: "Question B" });
  expect(screen.queryByText("Old node")).not.toBeInTheDocument();
  expect(
    screen.getByPlaceholderText(/Add architectural explanation/i),
  ).toHaveValue("");
  expect(
    screen.queryByRole("dialog", { name: "Cancel investigation" }),
  ).not.toBeInTheDocument();
});

it("keeps a late execution response associated with its submitted investigation", async () => {
  let finish!: (result: InvestigationDetailResponseV1) => void;
  vi.spyOn(api, "executeInvestigation").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const client = mount();
  await screen.findByRole("heading", { name: "Question A" });
  fireEvent.click(
    screen.getByRole("button", { name: "Execute Investigation" }),
  );
  fireEvent.click(screen.getByRole("button", { name: /Question B/ }));
  await screen.findByRole("heading", { name: "Question B" });
  await act(async () =>
    finish({
      summary: { ...summary(first, "Question A"), status: "completed" },
      claims: [],
    }),
  );
  await waitFor(() =>
    expect(
      client.getQueryData(["investigation-detail", repo, second]),
    ).toMatchObject({ summary: { id: second } }),
  );
  expect(screen.getByRole("heading", { name: "Question B" })).toBeVisible();
});

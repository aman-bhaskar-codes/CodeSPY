import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { MemoryReviewPanel } from "./MemoryReviewPanel";
import type { MemoryEntryV1 } from "../../api/generated/types.gen";

const entry: MemoryEntryV1 = {
  memory_id: "memory",
  org_id: "org",
  repository_id: "repo",
  snapshot_id: "snap",
  memory_kind: "architecture_fact",
  topic: "Scoped routing",
  summary: "Review this note",
  created_at: "2026-10-26T00:00:00Z",
  updated_at: "2026-10-26T00:00:00Z",
  state: "candidate",
  version: 1,
};
function mount() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryReviewPanel
        repositoryId="repo"
        snapshotId="snap"
        csrfToken="csrf"
      />
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
describe("memory review", () => {
  it("requires a review reason and sends the observed version", async () => {
    vi.spyOn(api, "memoryReviewPage").mockResolvedValue({
      items: [entry],
      can_review: true,
    });
    const review = vi
      .spyOn(api, "reviewMemory")
      .mockResolvedValue({ ...entry, state: "accepted", version: 2 });
    mount();
    fireEvent.click(
      await screen.findByRole("button", { name: "Accept Scoped routing" }),
    );
    expect(
      screen.getByRole("button", { name: "Confirm accept" }),
    ).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Review reason"), {
      target: { value: "Checked source" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm accept" }));
    await waitFor(() =>
      expect(review).toHaveBeenCalledWith(
        "repo",
        "memory",
        expect.objectContaining({
          action: "accept",
          expected_version: 1,
          reason: "Checked source",
          request_key: expect.any(String),
        }),
        "csrf",
      ),
    );
  });
  it("keeps review actions unavailable to readers", async () => {
    vi.spyOn(api, "memoryReviewPage").mockResolvedValue({
      items: [entry],
      can_review: false,
    });
    mount();
    await screen.findByText("Scoped routing");
    expect(
      screen.queryByRole("button", { name: "Accept Scoped routing" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/owner or administrator/i)).toBeInTheDocument();
  });
});

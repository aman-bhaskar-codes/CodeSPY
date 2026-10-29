import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { IndexBuildPanel } from "./IndexBuildPanel";

// Synthetic HTTP/component states; real worker publication is qualified separately.
const props = {
  repositoryId: "repo",
  snapshotId: "snapshot",
  csrfToken: "csrf",
};
const available = {
  repository_id: "repo",
  snapshot_id: "snapshot",
  chunk_set_id: "stored-set",
  index_id: "lexical-index",
  config_hash: "b".repeat(64),
  materialization_hash: "c".repeat(64),
  coverage: {
    admitted_files: 2,
    selected_files: 2,
    processed_files: 1,
    omissions: [{ reason: "source_limit", unit: "file", count: 1 }],
  },
  lexical_availability: "available",
  can_build: true,
} as const;
function mount() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      <IndexBuildPanel {...props} />
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  sessionStorage.clear();
});
describe("IndexBuildPanel", () => {
  it("distinguishes an unavailable index and offers an authorized build", async () => {
    vi.spyOn(api, "indexAvailability").mockResolvedValue({
      ...available,
      chunk_set_id: null,
      lexical_availability: "index_unavailable",
      coverage: null,
    } as never);
    mount();
    expect(
      await screen.findByText(/No published source index/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Build source index" }),
    ).toBeEnabled();
  });
  it("retains the published set during build and confirms cancellation", async () => {
    vi.spyOn(api, "indexAvailability").mockResolvedValue(available as never);
    vi.spyOn(api, "createIndex").mockResolvedValue({
      job: { id: "job", state: "queued" },
      status_url: "/status",
    } as never);
    vi.spyOn(api, "indexJob").mockResolvedValue({
      job: { id: "job", state: "running" },
      receipt: null,
      can_cancel: true,
      can_retry: false,
    } as never);
    const cancel = vi.spyOn(api, "cancelIndex").mockResolvedValue({
      job: { id: "job", state: "cancelled" },
      receipt: null,
      can_cancel: false,
      can_retry: true,
    } as never);
    mount();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Build source index" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Build source index" }));
    expect(
      await screen.findByText(/Build status: running/),
    ).toBeInTheDocument();
    expect(screen.getByText("stored-set")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel index build" }));
    expect(cancel).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm cancellation" }),
    );
    await waitFor(() => expect(cancel).toHaveBeenCalledOnce());
    expect(
      await screen.findByText(/Build status: cancelled/),
    ).toBeInTheDocument();
  });
  it("keeps a request key for retrying an uncertain build response", async () => {
    vi.spyOn(api, "indexAvailability").mockResolvedValue(available as never);
    const create = vi
      .spyOn(api, "createIndex")
      .mockRejectedValue(new Error("connection lost"));
    mount();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Build source index" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Build source index" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /connection lost/,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Retry build request" }),
    );
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[0][3]).toBe(create.mock.calls[1][3]);
  });
  it("uses server permission flags rather than offering a reader retry", async () => {
    vi.spyOn(api, "indexAvailability").mockResolvedValue({
      ...available,
      can_build: false,
    } as never);
    mount();
    expect(await screen.findByText("stored-set")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Build source index" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "Retry index build" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/1 of 2 admitted files/)).toBeInTheDocument();
  });
});

it("resumes a stored job and offers retry only when the server authorizes it", async () => {
  sessionStorage.setItem("codespy:index:repo:snapshot:job", "failed-job");
  vi.spyOn(api, "indexAvailability").mockResolvedValue(available as never);
  vi.spyOn(api, "indexJob").mockImplementation(
    async (_repo, _snapshot, job) =>
      ({
        job: {
          id: job,
          state: job === "failed-job" ? "failed" : "queued",
          failure_code: job === "failed-job" ? "index_timeout" : null,
        },
        receipt: null,
        can_cancel: job !== "failed-job",
        can_retry: job === "failed-job",
      }) as never,
  );
  const retry = vi.spyOn(api, "retryIndex").mockResolvedValue({
    retry_job_id: "retry-job",
    retry_of_job_id: "failed-job",
    index_spec_id: "spec",
  } as never);
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Retry index build" }),
  );
  expect(await screen.findByText(/Build status: queued/)).toBeInTheDocument();
  expect(retry).toHaveBeenCalledWith(
    "repo",
    "snapshot",
    "failed-job",
    "csrf",
    expect.any(String),
  );
  expect(sessionStorage.getItem("codespy:index:repo:snapshot:job")).toBe(
    "retry-job",
  );
});

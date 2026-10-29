import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";

it("loads findings from the published scan run rather than the job identity", async () => {
  const repo = "10000000-0000-4000-8000-000000000001",
    snapshot = "10000000-0000-4000-8000-000000000002",
    job = "10000000-0000-4000-8000-000000000003",
    run = "10000000-0000-4000-8000-000000000004";
  window.history.replaceState(
    {},
    "",
    `/?repository=${repo}&snapshot=${snapshot}&scan=${job}#findings-title`,
  );
  const fetcher = vi
    .fn()
    .mockImplementation((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            url.endsWith("/session")
              ? {
                  principal_id: "fixture",
                  csrf_token: "fixture",
                  auth_backend: "local_fixture",
                }
              : url.endsWith("/repositories")
                ? {
                    items: [
                      {
                        id: repo,
                        display_name: "Fixture",
                        source_kind: "archive",
                      },
                    ],
                  }
                : url.includes("/inventory")
                  ? { items: [] }
                  : url.endsWith(`/scan-jobs/${job}`)
                    ? { id: job, state: "succeeded", result_run_id: run }
                    : { code: "fixture_unavailable" },
          ),
          {
            status:
              url.includes("/scan-jobs/") ||
              url.endsWith("/session") ||
              url.endsWith("/repositories") ||
              url.includes("/inventory")
                ? 200
                : 503,
          },
        ),
      ),
    );
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith(
      expect.stringContaining(`/scans/${run}/summary`),
      expect.anything(),
    ),
  );
});

it("restores repository context on reload and browser history navigation", async () => {
  const first = "10000000-0000-4000-8000-000000000011";
  const second = "10000000-0000-4000-8000-000000000012";
  window.history.replaceState({}, "", `/?repository=${first}#upload-title`);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            url.endsWith("session")
              ? {
                  principal_id: "fixture",
                  csrf_token: "fixture-csrf",
                  auth_backend: "local_fixture",
                }
              : url.includes("/inventory")
                ? { items: [], next_cursor: null }
                : {
                    items: [first, second].map((id) => ({
                      id,
                      display_name: id === first ? "Alpha" : "Beta",
                      source_kind: "archive",
                    })),
                  },
          ),
        ),
      ),
    ),
  );
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  const selector = await screen.findByRole("combobox", { name: "Repository" });
  await waitFor(() => expect(selector).toHaveValue(first));
  act(() => {
    window.history.pushState({}, "", `/?repository=${second}#upload-title`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await waitFor(() => expect(selector).toHaveValue(second));
  expect(window.location.hash).toBe("#upload-title");
  act(() => {
    window.history.pushState(
      {},
      "",
      `/?repository=${second}&snapshot=10000000-0000-4000-8000-000000000099&scan=10000000-0000-4000-8000-000000000098#upload-title`,
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  fireEvent.change(selector, { target: { value: first } });
  await waitFor(() =>
    expect(window.location.search).not.toContain("snapshot="),
  );
  expect(window.location.search).not.toContain("scan=");
});

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/");
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});
it("explains an unavailable session without exposing server details", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ code: "provider_unavailable" }), {
        status: 503,
      }),
    ),
  );
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Sign-in is unavailable",
  );
  expect(
    screen.getByRole("heading", { name: "Workspace overview" }),
  ).toBeVisible();
});
it("shows the authorized empty repository state", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            url.endsWith("session")
              ? {
                  principal_id: "fixture",
                  csrf_token: "fixture-csrf",
                  auth_backend: "local_fixture",
                }
              : { items: [] },
          ),
        ),
      ),
    ),
  );
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  expect(
    await screen.findByText("No repositories are available."),
  ).toBeVisible();
});

it("follows the inventory child after admission succeeds", async () => {
  const parent = "10000000-0000-4000-8000-000000000001";
  const child = "10000000-0000-4000-8000-000000000002";
  window.history.replaceState({}, "", `/?job=${parent}`);
  const fetcher = vi.fn().mockImplementation((url: string) =>
    Promise.resolve(
      new Response(
        JSON.stringify(
          url.endsWith("session")
            ? {
                principal_id: "fixture",
                csrf_token: "fixture-csrf",
                auth_backend: "local_fixture",
              }
            : url.endsWith(parent)
              ? {
                  id: parent,
                  operation: "admit_source",
                  state: "succeeded",
                  inventory_job_id: child,
                  attempt_count: 1,
                  max_attempts: 3,
                }
              : url.endsWith(child)
                ? {
                    id: child,
                    operation: "inventory_snapshot",
                    state: "failed",
                    attempt_count: 1,
                    max_attempts: 3,
                    failure_code: "storage_unavailable",
                  }
                : { items: [] },
        ),
      ),
    ),
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Reason: storage unavailable")).toBeVisible();
  expect(screen.getByRole("button", { name: "Request retry" })).toBeEnabled();
});

it("allows corrected input after a terminal upload rejection without a job", async () => {
  const id = "10000000-0000-4000-8000-000000000003";
  window.history.replaceState({}, "", `/?submission=${id}`);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            url.endsWith("session")
              ? {
                  principal_id: "fixture",
                  csrf_token: "fixture-csrf",
                  auth_backend: "local_fixture",
                }
              : url.includes("submissions")
                ? {
                    id,
                    state: "rejected",
                    job_id: null,
                    error_code: "unsupported_format",
                  }
                : { items: [] },
          ),
        ),
      ),
    ),
  );
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  expect(
    await screen.findByRole("button", { name: "Start another submission" }),
  ).toBeEnabled();
});

it("reuses a retry command after a lost response", async () => {
  const userEvent = (await import("@testing-library/user-event")).default;
  const user = userEvent.setup();
  const id = "10000000-0000-4000-8000-000000000004";
  window.history.replaceState({}, "", `/?job=${id}`);
  const keys: string[] = [];
  const fetcher = vi
    .fn()
    .mockImplementation((url: string, options?: RequestInit) => {
      if (url.endsWith("/retry")) {
        keys.push(
          (options?.headers as Record<string, string>)["Idempotency-Key"],
        );
        if (keys.length === 1)
          return Promise.reject(new TypeError("synthetic lost response"));
        return Promise.resolve(
          new Response(
            JSON.stringify({
              retry_job_id: id,
              submission_id: "10000000-0000-4000-8000-000000000005",
              retry_of_job_id: id,
            }),
          ),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify(
            url.endsWith("session")
              ? {
                  principal_id: "fixture",
                  csrf_token: "fixture-csrf",
                  auth_backend: "local_fixture",
                }
              : url.includes("jobs/")
                ? {
                    id,
                    operation: "inventory_snapshot",
                    state: "failed",
                    attempt_count: 3,
                    max_attempts: 3,
                    failure_code: "storage_unavailable",
                  }
                : url.includes("submissions/")
                  ? { id, state: "admitted", job_id: id }
                  : { items: [] },
          ),
        ),
      );
    });
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  await user.click(
    await screen.findByRole("button", { name: "Request retry" }),
  );
  await screen.findByRole("alert");
  await user.click(screen.getByRole("button", { name: "Request retry" }));
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
});

it("displays structural architecture section and allows triggering analysis", async () => {
  const userEvent = (await import("@testing-library/user-event")).default;
  const user = userEvent.setup();
  const repoId = "10000000-0000-4000-8000-000000000010";
  const snapId = "10000000-0000-4000-8000-000000000020";
  window.history.replaceState(
    {},
    "",
    `/?repository=${repoId}&snapshot=${snapId}#structural-title`,
  );

  let analysisCreated = false;
  const fetcher = vi
    .fn()
    .mockImplementation((url: string, options?: RequestInit) => {
      if (url.endsWith("session")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              principal_id: "fixture",
              csrf_token: "fixture-csrf",
              auth_backend: "local_fixture",
            }),
          ),
        );
      }
      if (url.includes("/structural-report")) {
        return Promise.resolve(
          new Response(JSON.stringify({ code: "not_found" }), { status: 404 }),
        );
      }
      if (url.endsWith("/analyses") && options?.method === "POST") {
        analysisCreated = true;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              schema_version: 1,
              spec_id: "10000000-0000-4000-8000-000000000030",
              job_id: "10000000-0000-4000-8000-000000000040",
              snapshot_id: snapId,
            }),
            { status: 202 },
          ),
        );
      }
      if (url.includes("/investigations")) {
        return Promise.resolve(new Response(JSON.stringify({ items: [] })));
      }
      if (url.includes("/repositories")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [
                { id: repoId, display_name: "repo-1", default_branch: "main" },
              ],
            }),
          ),
        );
      }
      if (url.includes("/inventory")) {
        return Promise.resolve(new Response(JSON.stringify({ items: [] })));
      }
      return Promise.resolve(new Response(JSON.stringify({})));
    });

  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );

  // Structural Architecture section should appear
  expect(
    await screen.findByRole("heading", { name: "Structural Architecture" }),
  ).toBeVisible();

  const analyzeBtn = screen.getByRole("button", {
    name: "Analyze architecture",
  });
  expect(analyzeBtn).toBeEnabled();
  await user.click(analyzeBtn);

  expect(analysisCreated).toBe(true);
});

it("does not install a scan result after history changes the source context", async () => {
  const repo = "10000000-0000-4000-8000-000000000071",
    snapshot = "10000000-0000-4000-8000-000000000072",
    other = "10000000-0000-4000-8000-000000000073",
    job = "10000000-0000-4000-8000-000000000074";
  window.history.replaceState(
    {},
    "",
    `/?repository=${repo}&snapshot=${snapshot}#findings-title`,
  );
  let complete!: (value: Response) => void;
  const fetcher = vi.fn((url: string, options?: RequestInit) => {
    if (
      url.endsWith(`/snapshots/${snapshot}/scans`) &&
      options?.method === "POST"
    )
      return new Promise<Response>((resolve) => {
        complete = resolve;
      });
    return Promise.resolve(
      new Response(
        JSON.stringify(
          url.endsWith("/session")
            ? { principal_id: "fixture", csrf_token: "fixture" }
            : url.endsWith("/repositories")
              ? {
                  items: [repo, other].map((id) => ({
                    id,
                    display_name: id,
                    source_kind: "archive",
                  })),
                }
              : url.endsWith("scan-configuration")
                ? { configured: true, config: {}, live_access_verified: false }
                : { items: [] },
        ),
      ),
    );
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <App />
    </QueryClientProvider>,
  );
  const start = await screen.findByRole("button", { name: "Scan repository" });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  await waitFor(() => expect(complete).toBeDefined());
  act(() => {
    window.history.pushState({}, "", `/?repository=${other}#upload-title`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await act(async () =>
    complete(new Response(JSON.stringify({ job_id: job }))),
  );
  expect(window.location.search).toContain(other);
  expect(window.location.search).not.toContain(job);
});

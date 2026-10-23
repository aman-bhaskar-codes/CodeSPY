import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";

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
    screen.getByRole("heading", { name: "Source inventory" }),
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
  const fetcher = vi
    .fn()
    .mockImplementation((url: string) =>
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
    vi
      .fn()
      .mockImplementation((url: string) =>
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
  window.history.replaceState({}, "", `/?snapshot=${snapId}`);

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
        return Promise.resolve(
          new Response(JSON.stringify({ items: [] })),
        );
      }
      if (url.includes("/repositories")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [{ id: repoId, display_name: "repo-1", default_branch: "main" }],
            }),
          ),
        );
      }
      if (url.includes("/inventory")) {
        return Promise.resolve(
          new Response(JSON.stringify({ items: [] })),
        );
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

  // Wait for repository option to be loaded and select it
  await screen.findByRole("option", { name: /repo-1/ });
  const select = screen.getByRole("combobox", { name: "Repository" });
  await user.selectOptions(select, repoId);

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

import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { SessionBoundary } from "./SessionBoundary";
import { ApiError, request } from "../api/client";

afterEach(cleanup);
it("clears private cached data and unmounts the workspace after access expires", () => {
  const client = new QueryClient();
  client.setQueryData(["source-preview"], "private fixture source");
  render(
    <QueryClientProvider client={client}>
      <SessionBoundary>
        <p>Private workspace fixture</p>
      </SessionBoundary>
    </QueryClientProvider>,
  );
  act(() =>
    window.dispatchEvent(
      new CustomEvent("codespy:access-required", { detail: { status: 401 } }),
    ),
  );
  expect(
    screen.queryByText("Private workspace fixture"),
  ).not.toBeInTheDocument();
  expect(client.getQueryData(["source-preview"])).toBeUndefined();
  fireEvent.click(screen.getByRole("button", { name: "Reload access" }));
  expect(screen.getByText("Private workspace fixture")).toBeVisible();
});

it("drops a revoked read result without turning a write denial into sign-out", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(["finding"], "Previously authorized result");
  render(
    <QueryClientProvider client={client}>
      <SessionBoundary>
        <p>Workspace still mounted</p>
      </SessionBoundary>
    </QueryClientProvider>,
  );
  await act(async () => {
    await client
      .fetchQuery({
        queryKey: ["finding"],
        queryFn: () =>
          Promise.reject(new ApiError(404, "resource_unavailable")),
      })
      .catch(() => {});
  });
  expect(client.getQueryData(["finding"])).toBeUndefined();
  expect(screen.getByText("Workspace still mounted")).toBeVisible();
});

it("clears the workspace when a refreshed session belongs to another principal", () => {
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <SessionBoundary>
        <p>Private workspace fixture</p>
      </SessionBoundary>
    </QueryClientProvider>,
  );
  act(() => client.setQueryData(["session"], { principal_id: "first" }));
  act(() => client.setQueryData(["session"], { principal_id: "second" }));
  expect(
    screen.queryByText("Private workspace fixture"),
  ).not.toBeInTheDocument();
});

it("fences pending transport results across access reset and reload", async () => {
  let complete!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          complete = resolve;
        }),
    ),
  );
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <SessionBoundary>
        <p>Private fixture</p>
      </SessionBoundary>
    </QueryClientProvider>,
  );
  const pending = request("/investigation-fixture");
  act(() => window.dispatchEvent(new Event("codespy:access-required")));
  fireEvent.click(screen.getByRole("button", { name: "Reload access" }));
  complete(new Response(JSON.stringify({ summary: "Old private report" })));
  await expect(pending).rejects.toMatchObject({
    code: "access_context_changed",
  });
  expect(client.getQueryData(["investigation-detail"])).toBeUndefined();
  vi.unstubAllGlobals();
});

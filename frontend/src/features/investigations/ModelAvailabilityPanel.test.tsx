import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { ModelAvailabilityPanel } from "./ModelAvailabilityPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
it("shows server-reported qualification without claiming provider connectivity", async () => {
  vi.spyOn(api, "modelAvailability").mockResolvedValue({
    live_access_verified: false,
    profiles: [
      {
        alias: "openai_fast",
        provider: "openai",
        enabled: true,
        eligible: true,
        locally_qualified: true,
        owner_approved_live: false,
        status: "live_unqualified",
        supports_tools: true,
        supports_native_schema: false,
      },
    ],
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ModelAvailabilityPanel repositoryId="repo" />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("openai_fast")).toBeVisible();
  expect(
    screen.getByText(
      "Live access is not verified. Configuration status does not prove a provider call will succeed.",
    ),
  ).toBeVisible();
});

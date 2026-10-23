import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InvestigationsWorkbench } from "./InvestigationsWorkbench";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}

describe("InvestigationsWorkbench", () => {
  const defaultProps = {
    repositoryId: "00000000-0000-0000-0000-000000000001",
    snapshotId: "00000000-0000-0000-0000-000000000002",
    csrfToken: "csrf-token-test",
  };

  it("renders the workbench heading, description, and action controls", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (url.includes("/investigations")) {
          return Promise.resolve(
            new Response(JSON.stringify({ items: [] }), { status: 200 }),
          );
        }
        return Promise.reject(new Error("unhandled"));
      }),
    );

    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);

    expect(
      screen.getByText(/Agentic Security Investigations/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Evidence-grounded model orchestration, anti-hallucination claim verification/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Run Investigation/i }),
    ).toBeInTheDocument();
  });

  it("allows setting target finding ID and max cost budget", () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ items: [] }), { status: 200 }),
        ),
    );

    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);

    const findingInput = screen.getByLabelText(/Target Finding ID/i);
    fireEvent.change(findingInput, { target: { value: "finding-sqli-01" } });
    expect(findingInput).toHaveValue("finding-sqli-01");

    const costSelect = screen.getByLabelText(/Max Cost Budget/i);
    fireEvent.change(costSelect, { target: { value: "250000" } });
    expect(costSelect).toHaveValue("250000");
  });

  it("renders empty state placeholder when no investigation is selected", () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ items: [] }), { status: 200 }),
        ),
    );

    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);

    expect(
      screen.getByText(
        /Select an investigation from the left or launch a new hypothesis/i,
      ),
    ).toBeInTheDocument();
  });
});

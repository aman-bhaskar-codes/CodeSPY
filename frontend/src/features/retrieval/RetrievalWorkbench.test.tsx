import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RetrievalWorkbench } from "./RetrievalWorkbench";

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
    <QueryClientProvider client={queryClient}>
      {ui}
    </QueryClientProvider>
  );
}

describe("RetrievalWorkbench", () => {
  const defaultProps = {
    repositoryId: "00000000-0000-0000-0000-000000000001",
    snapshotId: "00000000-0000-0000-0000-000000000002",
    csrfToken: "csrf-token-12345",
  };

  it("renders the primary workbench title, description, and navigation tabs", () => {
    renderWithClient(<RetrievalWorkbench {...defaultProps} />);

    expect(screen.getByText(/Source Retrieval & Grounding Workbench/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Multi-Modal Search/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Context Engineering/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Repository Memory/i })).toBeInTheDocument();
  });

  it("allows switching search channels (hybrid, lexical, dense, exact)", () => {
    renderWithClient(<RetrievalWorkbench {...defaultProps} />);

    expect(screen.getByLabelText(/Hybrid \(RRF Fusion\)/i)).toBeChecked();

    const lexicalRadio = screen.getByLabelText(/Lexical \(BM25\)/i);
    fireEvent.click(lexicalRadio);
    expect(lexicalRadio).toBeChecked();

    const exactRadio = screen.getByLabelText(/Exact \(AST\/Symbol\)/i);
    fireEvent.click(exactRadio);
    expect(exactRadio).toBeChecked();
    expect(screen.getByPlaceholderText(/Exact symbol or filepath/i)).toBeInTheDocument();
  });

  it("renders context engineering controls when switching to Context tab", () => {
    renderWithClient(<RetrievalWorkbench {...defaultProps} />);

    const contextTab = screen.getByRole("button", { name: /Context Engineering/i });
    fireEvent.click(contextTab);

    expect(screen.getByText(/Max Token Budget/i)).toBeInTheDocument();
    expect(screen.getByText(/Reserved Overhead Tokens/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Assemble Grounded Context Packet/i })).toBeInTheDocument();
  });

  it("renders repository memory creation form and query input when switching to Memory tab", () => {
    renderWithClient(<RetrievalWorkbench {...defaultProps} />);

    const memoryTab = screen.getByRole("button", { name: /Repository Memory/i });
    fireEvent.click(memoryTab);

    expect(screen.getByPlaceholderText(/Query repository memories by topic/i)).toBeInTheDocument();
    expect(screen.getByText(/Record Grounded Memory/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/e\.g\. auth-timing-invariance/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save Memory/i })).toBeInTheDocument();
  });
});

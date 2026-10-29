import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RetrievalWorkbench } from "./RetrievalWorkbench";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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

describe("RetrievalWorkbench", () => {
  const defaultProps = {
    repositoryId: "00000000-0000-0000-0000-000000000001",
    snapshotId: "00000000-0000-0000-0000-000000000002",
    csrfToken: "csrf-token-12345",
  };

  it("renders the primary workbench title, description, and navigation tabs", () => {
    renderWithClient(<RetrievalWorkbench {...defaultProps} />);

    expect(
      screen.getByText(/Search, context & memory/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /Code search/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /Context/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /Memory/i }),
    ).toBeInTheDocument();
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
    expect(
      screen.getByPlaceholderText(/Exact symbol or filepath/i),
    ).toBeInTheDocument();
  });

  it("renders context engineering controls when switching to Context tab", async () => {
    renderWithClient(<RetrievalWorkbench {...defaultProps} />);

    const contextTab = screen.getByRole("tab", {
      name: /Context/i,
    });
    await userEvent.click(contextTab);
    expect(contextTab).toHaveAttribute("aria-selected", "true");

    expect(screen.getByText(/Max Token Budget/i)).toBeInTheDocument();
    expect(screen.getByText(/Reserved Overhead Tokens/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Assemble Grounded Context Packet/i }),
    ).toBeInTheDocument();
  });

  it("renders repository memory creation form and query input when switching to Memory tab", async () => {
    renderWithClient(<RetrievalWorkbench {...defaultProps} />);

    const memoryTab = screen.getByRole("tab", {
      name: /Memory/i,
    });
    await userEvent.click(memoryTab);
    expect(memoryTab).toHaveAttribute("aria-selected", "true");

    expect(
      screen.getByPlaceholderText(/Query repository memories by topic/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Record Grounded Memory/i)).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(/e\.g\. auth-timing-invariance/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Submit memory for review/i }),
    ).toBeInTheDocument();
  });
});

it("preserves qualified results during a new search and pins context to their set", async () => {
  const { api } = await import("../../api/client");
  const hit = {
    chunk_id: "chunk",
    file_id: "file",
    file_path: "main.py",
    chunk_kind: "file",
    start_byte: 0,
    end_byte: 4,
    start_line: 1,
    end_line: 1,
    score: 1,
    snippet: "pass",
    content_sha256: "a".repeat(64),
    chunk_set_id: "old-set",
    config_hash: "b".repeat(64),
    materialization_hash: "c".repeat(64),
    safe_content_sha256: "d".repeat(64),
    match_type: "path",
  };
  vi.spyOn(api, "indexAvailability").mockResolvedValue({
    can_build: false,
    chunk_set_id: "old-set",
    lexical_availability: "available",
  } as never);
  vi.spyOn(api, "queryMemories").mockResolvedValue({
    items: [],
    total_candidates: 0,
  } as never);
  vi.spyOn(api, "searchExact")
    .mockResolvedValueOnce({
      query: "first",
      hits: [hit],
      total_candidates: 1,
      execution_ms: 1,
      chunk_set_id: "old-set",
      lexical_availability: "available",
    } as never)
    .mockImplementationOnce(() => new Promise(() => {}));
  const assemble = vi.spyOn(api, "assembleContext").mockResolvedValue({
    packet_id: "packet",
    query: "first",
    items: [],
    items_count: 0,
    total_tokens: 0,
    total_bytes: 0,
    omitted_count: 0,
    packet_sha256: "e".repeat(64),
    chunk_set_id: "old-set",
  } as never);
  renderWithClient(
    <RetrievalWorkbench
      repositoryId="repo"
      snapshotId="snapshot"
      csrfToken="csrf"
    />,
  );
  fireEvent.click(screen.getByLabelText(/Exact \(AST\/Symbol\)/));
  const query = screen.getByRole("textbox", { name: "Search admitted source" });
  fireEvent.change(query, { target: { value: "first" } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(await screen.findByText(/Results for “first”/)).toBeInTheDocument();
  fireEvent.change(query, { target: { value: "second" } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(screen.getByText(/Results for “first”/)).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: /Pack Top Hits into Context Packet/ }),
  );
  await screen.findByText(/Context set:/);
  expect(assemble.mock.calls[0][2]).toMatchObject({
    query: "first",
    chunk_set_id: "old-set",
    candidate_hits: [
      {
        chunk_kind: "file",
        chunk_set_id: "old-set",
        safe_content_sha256: "d".repeat(64),
        match_modalities: ["path"],
      },
    ],
  });
});

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api } from "../../api/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InvestigationsWorkbench } from "./InvestigationsWorkbench";

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

describe("InvestigationsWorkbench", () => {
  const defaultProps = {
    repositoryId: "00000000-0000-0000-0000-000000000001",
    snapshotId: "00000000-0000-0000-0000-000000000002",
    csrfToken: "csrf-token-test",
  };

  it("binds a finding investigation to its published scan run", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }))));
    const create = vi.spyOn(api, "createInvestigation").mockRejectedValue(new Error("fixture stop"));
    const finding = "10000000-0000-4000-8000-000000000001", run = "10000000-0000-4000-8000-000000000002";
    renderWithClient(<InvestigationsWorkbench {...defaultProps} findingId={finding} findingRunId={run} />);
    fireEvent.change(screen.getByLabelText(/Question or Finding/i), { target: { value: "Review this source observation" } });
    fireEvent.click(screen.getByRole("button", { name: /Create investigation/i }));
    await waitFor(() => expect(create).toHaveBeenCalledWith(defaultProps.repositoryId, expect.objectContaining({ target_type: "finding", finding_id: finding, finding_run_id: run }), defaultProps.csrfToken, expect.any(String)));
  });

  it("reuses a creation request key after an uncertain response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }))));
    const create = vi.spyOn(api, "createInvestigation").mockRejectedValue(new Error("Network uncertain"));
    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);
    fireEvent.change(screen.getByLabelText(/Question/i), { target: { value: "Check retained source" } });
    fireEvent.click(screen.getByRole("button", { name: /Create investigation/i }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByRole("button", { name: /Create investigation/i })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: /Create investigation/i }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[0][3]).toEqual(expect.any(String));
    expect(create.mock.calls[1][3]).toBe(create.mock.calls[0][3]);
  });

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
      screen.getByText(/Investigation workbench/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Investigate questions with source evidence/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Create investigation/i }),
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

  it("renders cancel button and triggers cancel mutation for running investigation", async () => {
    const invId = "11111111-1111-1111-1111-111111111111";
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes(`/investigations/${invId}/cancel`)) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              id: invId,
              status: "cancelled",
              cancelled_at: new Date().toISOString(),
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes(`/investigations/${invId}`)) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              summary: {
                id: invId,
                status: "running",
                query: "Detect unsafe deserialization",
                current_step: 3,
                max_steps: 10,
                spent_micros: 1_200_000,
                held_micros: 800_000,
                budget_limit_micros: 5_000_000,
                created_at: new Date().toISOString(),
              },
              claims: [],
              events: [],
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/investigations")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [
                {
                  id: invId,
                  status: "running",
                  query: "Detect unsafe deserialization",
                  current_step: 3,
                  max_steps: 10,
                  spent_micros: 1_200_000,
                  held_micros: 800_000,
                },
              ],
              total_count: 1,
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ items: [] }), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);

    // Select the investigation from the list
    const item = await screen.findByText(/Detect unsafe deserialization/i);
    fireEvent.click(item);

    // Cancel button should appear
    const cancelBtn = await screen.findByRole("button", { name: /Cancel/i });
    expect(cancelBtn).toBeInTheDocument();

    // Verify held and spent metrics
    expect(screen.getByText("$1.2000")).toBeInTheDocument();
    expect(screen.getByText("$0.8000")).toBeInTheDocument();

    // Click cancel
    fireEvent.click(cancelBtn);
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining(`/investigations/${invId}/cancel`), expect.anything());
    fireEvent.click(await screen.findByRole("button", { name: "Confirm cancellation" }));

    // Verify cancel endpoint was called
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/investigations/${invId}/cancel`),
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("renders human decision banner and resume button for awaiting_human_decision status", async () => {
    const invId = "22222222-2222-2222-2222-222222222222";
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes(`/investigations/${invId}/resume`)) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              investigation_id: invId,
              job_id: "33333333-3333-3333-3333-333333333333",
              spec_hash: "hash123",
              status: "running",
              reused_checkpoint: true,
            }),
            { status: 202 },
          ),
        );
      }
      if (url.includes(`/investigations/${invId}`)) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              summary: {
                id: invId,
                status: "awaiting_human_decision",
                current_job_id: "job-failed-id",
                spec_hash: "spec-hash-123",
                query: "Sensitive data leak review",
                current_step: 4,
                max_steps: 10,
                spent_micros: 500_000,
                budget_limit_micros: 2_000_000,
                created_at: new Date().toISOString(),
              },
              claims: [],
              events: [],
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/investigations")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [
                {
                  id: invId,
                  status: "awaiting_human_decision",
                  query: "Sensitive data leak review",
                  current_step: 4,
                  max_steps: 10,
                  spent_micros: 500_000,
                },
              ],
              total_count: 1,
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ items: [] }), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);

    // Select the investigation
    const item = await screen.findByText(/Sensitive data leak review/i);
    fireEvent.click(item);

    // Verify human decision banner and resume button
    expect(
      await screen.findByText(/Human Decision Required/i),
    ).toBeInTheDocument();
    const resumeBtn = screen.getByRole("button", { name: /Resume/i });
    expect(resumeBtn).toBeInTheDocument();

    // Click resume
    fireEvent.click(resumeBtn);

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/investigations/${invId}/resume`),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            expected_failed_job_id: "job-failed-id",
            expected_spec_hash: "spec-hash-123",
          }),
        }),
      );
    });
  });

  it("renders retry button when status is failed or cancelled", async () => {
    const invId = "44444444-4444-4444-4444-444444444444";
    const newInvId = "55555555-5555-5555-5555-555555555555";
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes(`/investigations/${invId}/retry`)) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              id: newInvId,
              status: "pending",
              retry_of_id: invId,
              query: "Failed SQL injection investigation",
              budget_limit_micros: 5_000_000,
              max_steps: 10,
              created_at: new Date().toISOString(),
            }),
            { status: 201 },
          ),
        );
      }
      if (url.includes(`/investigations/${invId}`)) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              summary: {
                id: invId,
                status: "failed",
                failure_reason: "Model budget exhaustion",
                query: "Failed SQL injection investigation",
                current_step: 8,
                max_steps: 10,
                spent_micros: 5_000_000,
                budget_limit_micros: 5_000_000,
                created_at: new Date().toISOString(),
              },
              claims: [],
              events: [],
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/investigations")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [
                {
                  id: invId,
                  status: "failed",
                  query: "Failed SQL injection investigation",
                  current_step: 8,
                  max_steps: 10,
                  spent_micros: 5_000_000,
                },
              ],
              total_count: 1,
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ items: [] }), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);

    // Select the failed investigation
    const item = await screen.findByText(/Failed SQL injection/i);
    fireEvent.click(item);

    // Verify failure banner and retry button
    expect(
      await screen.findByText(/Model budget exhaustion/i),
    ).toBeInTheDocument();
    const retryBtn = screen.getByRole("button", {
      name: /Retry Investigation/i,
    });
    expect(retryBtn).toBeInTheDocument();

    // Click retry
    fireEvent.click(retryBtn);

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/investigations/${invId}/retry`),
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("renders coverage notes and citations when present in report", async () => {
    const invId = "66666666-6666-6666-6666-666666666666";
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes(`/investigations/${invId}`)) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              summary: {
                id: invId,
                status: "completed",
                query: "Auth token validation",
                verdict: "true_positive",
                verdict_confidence: 0.95,
                summary: "Hardcoded secret found in auth middleware",
                current_step: 4,
                max_steps: 10,
                spent_micros: 150_000,
                budget_limit_micros: 1_000_000,
                created_at: new Date().toISOString(),
              },
              report: {
                verdict: "true_positive",
                claims: [
                  {
                    claim_id: "claim-1",
                    statement: "JWT signing secret is hardcoded",
                    status: "verified",
                    file_path: "src/auth.ts",
                    line_start: 42,
                    quoted_source: "const SECRET = 'dev-secret';",
                  },
                ],
                verifications: [{ claim_id: "claim-1", status: "source_correspondence_confirmed", resolved_file: "src/auth.ts", resolved_line_start: 42 }],
                evidence_refs: ["evidence-1"],
                coverage_notes: [
                  "Checked auth controller",
                  "Checked token validation middleware",
                ],
                citations: [
                  {
                    path: "src/auth.ts",
                    line: 42,
                    statement: "Secret literal declared",
                    snippet: "const SECRET = 'dev-secret';",
                  },
                ],
              },
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/investigations")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [
                {
                  id: invId,
                  status: "completed",
                  query: "Auth token validation",
                  verdict: "true_positive",
                  current_step: 4,
                  max_steps: 10,
                  spent_micros: 150_000,
                },
              ],
              total_count: 1,
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ items: [] }), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithClient(<InvestigationsWorkbench {...defaultProps} />);

    // Select the completed investigation
    const item = await screen.findByText(/Auth token validation/i);
    fireEvent.click(item);

    // Check verdict and confidence
    expect(
      await screen.findByText(/VERDICT: TRUE POSITIVE/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Reported confidence: 95%/i)).toBeInTheDocument();

    // Check claims
    expect(
      screen.getByText(/JWT signing secret is hardcoded/i),
    ).toBeInTheDocument();
    expect(screen.getByText("SOURCE CORRESPONDENCE (AT PUBLICATION)")).toBeInTheDocument();

    // Check coverage notes
    expect(screen.getByText(/Checked auth controller/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Checked token validation middleware/i),
    ).toBeInTheDocument();

    // Check citations
    expect(
      screen.getByText(/Citations & References \(1\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Secret literal declared/i)).toBeInTheDocument();
  });
});

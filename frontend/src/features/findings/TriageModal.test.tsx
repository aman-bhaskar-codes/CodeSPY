import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { TriageModal } from "./TriageModal";
import type { FindingSummaryV1 } from "./types";

afterEach(cleanup);

it("reuses the request key after an uncertain submission failure", async () => {
  const finding: FindingSummaryV1 = {
    id: "finding",
    run_id: "run",
    category: "source_pattern",
    rule_id: "rule",
    severity: "high",
    title: "Fixture",
    file_path: "src/a.py",
    disposition: "unresolved",
    triage_state: "unreviewed",
    triage_reason: "none",
    triage_version: 0,
  };
  const submit = vi.fn().mockRejectedValue(new Error("network interrupted"));
  render(
    <TriageModal
      finding={finding}
      isOpen
      onClose={() => {}}
      onSubmit={submit}
    />,
  );
  fireEvent.submit(
    screen
      .getByRole("button", { name: /save|record|submit/i })
      .closest("form")!,
  );
  await screen.findByText("network interrupted");
  fireEvent.submit(
    screen
      .getByRole("button", { name: /save|record|submit/i })
      .closest("form")!,
  );
  await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
  const first = submit.mock.calls[0];
  const second = submit.mock.calls[1];
  expect(first[1]).toEqual(expect.any(String));
  expect(first[1]).toBe(second[1]);
  expect(first[0]).toEqual(second[0]);
});

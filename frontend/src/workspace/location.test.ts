import { afterEach, expect, it } from "vitest";
import { selectedInvestigation, rememberInvestigation } from "./location";

afterEach(() => window.history.replaceState({}, "", "/"));
it("restores only a valid investigation in the selected repository and snapshot", () => {
  const repo = "10000000-0000-4000-8000-000000000001", snapshot = "10000000-0000-4000-8000-000000000002", investigation = "10000000-0000-4000-8000-000000000003";
  window.history.replaceState({}, "", `/?repository=${repo}&snapshot=${snapshot}#workbench-title`);
  rememberInvestigation(investigation);
  expect(selectedInvestigation(repo, snapshot)).toBe(investigation);
  expect(selectedInvestigation("other", snapshot)).toBeNull();
  expect(selectedInvestigation(repo, "other")).toBeNull();
  rememberInvestigation("not-an-id");
  expect(window.location.search).not.toContain("not-an-id");
});

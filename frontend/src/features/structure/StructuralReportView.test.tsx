import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StructuralReportView } from "./StructuralReportView";
import fixture from "./fixtures/report.json";
import type { ArchitectureReportV1 } from "../../api/generated/types.gen";

afterEach(cleanup);
it("shows exact stored source as escaped text", () => {
  const report = structuredClone(fixture) as ArchitectureReportV1;
  report.excerpts![0]!.text = '<img src=x onerror="alert(1)">';
  const { container } = render(
    <StructuralReportView report={report} repositoryId="fixture" />,
  );
  fireEvent.click(
    screen.getAllByRole("button", { name: /Inspect source/ })[0]!,
  );
  expect(
    screen.getByRole("region", { name: "Source excerpt" }),
  ).toHaveTextContent("<img src=x");
  expect(container.querySelector("img")).toBeNull();
  expect(
    screen.getByText(/Static candidates do not prove runtime calls/),
  ).toBeVisible();
});
it("keeps partial coverage visible and allows keyboard-accessible state filtering", () => {
  const report = structuredClone(fixture) as ArchitectureReportV1;
  report.coverage = { ...report.coverage, syntax: "partial", omitted_files: 1 };
  render(<StructuralReportView report={report} repositoryId="fixture" />);
  expect(screen.getByRole("status")).toHaveTextContent("Partial coverage");
  fireEvent.change(screen.getByRole("combobox", { name: "Binding state" }), {
    target: { value: "unresolved" },
  });
  expect(screen.getByRole("combobox")).toHaveValue("unresolved");
});

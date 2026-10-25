import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { FindingSupport } from "./FindingSupport";

afterEach(cleanup);
it("separates current support from history and renders source as text", () => {
  const { container } = render(
    <FindingSupport
      category="source_pattern"
      disposition="unresolved"
      historicalDisposition="supported-static"
      supportReason="source_unavailable"
      excerpt="<script>bad()</script>"
      excerptReason="available"
    />,
  );
  expect(screen.getByText("Current support")).toBeInTheDocument();
  expect(screen.getByText("Historical decision")).toBeInTheDocument();
  expect(
    screen.getByText("Source support is no longer available."),
  ).toBeInTheDocument();
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("code")?.textContent).toBe(
    "<script>bad()</script>",
  );
});
it("never displays an excerpt for a secret-format finding", () => {
  render(
    <FindingSupport
      category="secret_format"
      disposition="supported-static"
      historicalDisposition="supported-static"
      supportReason="current_support"
      excerpt="canary-must-not-render"
      excerptReason="available"
    />,
  );
  expect(screen.queryByText("canary-must-not-render")).toBeNull();
  expect(
    screen.getByText("Secret-format matches are hidden."),
  ).toBeInTheDocument();
});
it("explains missing excerpts without implying a clean result", () => {
  render(
    <FindingSupport
      category="source_pattern"
      disposition="unresolved"
      historicalDisposition="supported-static"
      supportReason="bundle_unavailable"
      excerpt={null}
      excerptReason="reader_unavailable"
    />,
  );
  expect(
    screen.getByText("The isolated source reader is unavailable."),
  ).toBeInTheDocument();
});

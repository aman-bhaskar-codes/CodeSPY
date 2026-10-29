import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { GitHubImportForm } from "./GitHubImportForm";

afterEach(cleanup);
it("submits an explicit ref and prevents duplicate pending activation", () => {
  const submit = vi.fn();
  const { rerender } = render(<GitHubImportForm pending={false} onImport={submit} />);
  fireEvent.change(screen.getByLabelText("Git reference"), { target: { value: " main " } });
  fireEvent.click(screen.getByRole("button", { name: "Import from GitHub" }));
  expect(submit).toHaveBeenCalledWith("main");
  rerender(<GitHubImportForm pending onImport={submit} />);
  expect(screen.getByRole("button", { name: "Import from GitHub" })).toBeDisabled();
});

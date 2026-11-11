import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { GitHubUrlImportForm } from "./GitHubUrlImportForm";

afterEach(cleanup);
it("submits a trimmed public GitHub URL and blocks pending activation", () => {
  const submit = vi.fn();
  const { rerender } = render(<GitHubUrlImportForm pending={false} onImport={submit} />);
  fireEvent.change(screen.getByLabelText("Public repository URL"), { target: { value: " https://github.com/expressjs/express " } });
  fireEvent.click(screen.getByRole("button", { name: "Import public repository" }));
  expect(submit).toHaveBeenCalledWith("https://github.com/expressjs/express");
  rerender(<GitHubUrlImportForm pending onImport={submit} />);
  expect(screen.getByRole("button", { name: "Import public repository" })).toBeDisabled();
});

it.each(["", "http://github.com/o/r", "https://evil.test/o/r", "https://user@github.com/o/r", "https://github.com/o"])("rejects %j without calling the server", value => {
  const submit = vi.fn();
  render(<GitHubUrlImportForm pending={false} onImport={submit} />);
  fireEvent.change(screen.getByLabelText("Public repository URL"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Import public repository" }));
  expect(submit).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toBeInTheDocument();
});

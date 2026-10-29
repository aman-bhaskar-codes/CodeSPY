import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { WorkspaceShell, WorkspacePane } from "./WorkspaceShell";

afterEach(cleanup);

it("focuses a selected pane without unmounting another pane's draft", () => {
  window.history.replaceState({}, "", "/#inventory-title");
  render(<WorkspaceShell sessionState="available" destinations={[]}>
    <WorkspacePane id="upload-title"><label>Draft note<input defaultValue="Keep me" /></label></WorkspacePane>
    <WorkspacePane id="inventory-title"><h2>Files view</h2></WorkspacePane>
  </WorkspaceShell>);
  expect(screen.getByText("Files view")).toBeVisible();
  expect(screen.getByLabelText("Draft note")).not.toBeVisible();
  act(() => { window.history.replaceState({}, "", "/#upload-title"); window.dispatchEvent(new HashChangeEvent("hashchange")); });
  expect(screen.getByLabelText("Draft note")).toBeVisible();
  expect(screen.getByLabelText("Draft note")).toHaveValue("Keep me");
  window.history.replaceState({}, "", "/");
});

it("offers real section navigation without enabling unavailable destinations", () => {
  render(
    <WorkspaceShell
      sessionState="unavailable"
      destinations={[
        { id: "workspace-home", label: "Overview", enabled: true },
        { id: "inventory-title", label: "Files", enabled: false },
      ]}
    >
      <h1 id="workspace-home">Source inventory</h1>
    </WorkspaceShell>,
  );
  expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute(
    "href",
    "#workspace-home",
  );
  expect(screen.getByRole("link", { name: "CodeSPY home" })).toHaveAttribute(
    "href",
    "#workspace-home",
  );
  expect(screen.queryByRole("link", { name: "Files" })).not.toBeInTheDocument();
  expect(screen.getByText("Files").closest("[aria-disabled]")).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(
    screen.getByRole("link", { name: "Skip to workspace" }),
  ).toHaveAttribute("href", "#workspace-content");
  expect(screen.getByRole("main")).toContainElement(
    screen.getByRole("heading", { name: "Source inventory" }),
  );
});

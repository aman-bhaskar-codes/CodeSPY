import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { useOpenFiles } from "../features/source/useOpenFiles";
import { EditorTabs, usePaneTabs } from "./EditorTabs";

const labels = {
  "workspace-home": "Overview",
  "inventory-title": "Files",
  "source-title": "Source",
};

beforeEach(() => {
  usePaneTabs.setState({ panes: [] });
  useOpenFiles.getState().reset();
  window.history.replaceState({}, "", "/");
});
afterEach(cleanup);

function strip() {
  return screen.queryByRole("tablist", { name: "Open editors" });
}

it("shows nothing until a view is visited, then adds the active view", () => {
  const { rerender } = render(
    <EditorTabs active="source-title" labels={labels} />,
  );
  expect(strip()).not.toBeInTheDocument();
  rerender(<EditorTabs active="workspace-home" labels={labels} />);
  expect(screen.getByRole("tab", { name: /Overview/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  rerender(<EditorTabs active="inventory-title" labels={labels} />);
  expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
    "Overview",
    "Files",
  ]);
  expect(screen.getByRole("tab", { name: /Overview/ })).toHaveAttribute(
    "aria-selected",
    "false",
  );
});

it("lists open files, selects them and closes with a fallback", () => {
  act(() => {
    useOpenFiles.getState().open({ fileId: "1", path: "src/a.py" });
    useOpenFiles.getState().open({ fileId: "2", path: "src/b.py" });
  });
  render(<EditorTabs active="source-title" labels={labels} />);
  const tabs = screen.getAllByRole("tab");
  expect(tabs.map((t) => t.textContent)).toEqual(["a.py", "b.py"]);
  expect(screen.getByRole("tab", { name: /b\.py/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  fireEvent.click(screen.getByRole("tab", { name: /a\.py/ }));
  expect(useOpenFiles.getState().activeId).toBe("1");
  expect(window.location.hash).toBe("#source-title");
  fireEvent.click(
    screen.getByRole("tab", { name: /a\.py/ }).querySelector("[data-close]")!,
  );
  expect(useOpenFiles.getState().files.map((f) => f.fileId)).toEqual(["2"]);
  expect(useOpenFiles.getState().activeId).toBe("2");
});

it("closing the last active tab returns to the overview", () => {
  act(() => useOpenFiles.getState().open({ fileId: "1", path: "a.py" }));
  render(<EditorTabs active="source-title" labels={labels} />);
  fireEvent.click(
    screen.getByRole("tab", { name: /a\.py/ }).querySelector("[data-close]")!,
  );
  expect(window.location.hash).toBe("#workspace-home");
});

it("supports roving keyboard navigation and Delete to close", () => {
  act(() =>
    usePaneTabs.setState({ panes: ["workspace-home", "inventory-title"] }),
  );
  render(<EditorTabs active="workspace-home" labels={labels} />);
  const list = screen.getByRole("tablist");
  const first = within(list).getByRole("tab", { name: /Overview/ });
  first.focus();
  expect(first).toHaveAttribute("tabindex", "0");
  expect(within(list).getByRole("tab", { name: /Files/ })).toHaveAttribute(
    "tabindex",
    "-1",
  );
  fireEvent.keyDown(list, { key: "ArrowRight" });
  expect(document.activeElement).toHaveTextContent("Files");
  fireEvent.keyDown(list, { key: "End" });
  expect(document.activeElement).toHaveTextContent("Files");
  fireEvent.keyDown(list, { key: "Delete" });
  expect(usePaneTabs.getState().panes).toEqual(["inventory-title"]);
});

import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { useOpenFiles } from "../source/useOpenFiles";
import { FileTree } from "./FileTree";
import { visibleRows } from "./tree";

const entry = (id: string, path: string, eligible = true) => ({
  id,
  path,
  eligible,
  exclusion_reason: null,
  language: null,
  size_bytes: 1,
  sha256: "a".repeat(64),
});

function mount(pages: Record<string, unknown>) {
  vi.spyOn(api, "inventory").mockImplementation(
    async (_snapshot, cursor) => pages[String(cursor)] as never,
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <FileTree repositoryId="r" snapshotId="s" />
    </QueryClientProvider>,
  );
}

beforeEach(() => useOpenFiles.getState().reset());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});

describe("visibleRows", () => {
  it("lists directories first, sorted, and only expanded children", () => {
    const files = [
      { id: "1", path: "z.py", eligible: true },
      { id: "2", path: "a/b/c.py", eligible: true },
      { id: "3", path: "a/d.py", eligible: true },
    ];
    expect(visibleRows(files, new Set()).map((r) => r.name)).toEqual([
      "a",
      "z.py",
    ]);
    expect(visibleRows(files, new Set(["a/"])).map((r) => r.name)).toEqual([
      "a",
      "b",
      "d.py",
      "z.py",
    ]);
  });
});

describe("FileTree", () => {
  const pages = {
    null: {
      items: [
        entry("1", "src/app.py"),
        entry("2", "src/util/x.py"),
        entry("3", "README.md", false),
      ],
      next_cursor: "c2",
    },
    c2: { items: [entry("4", "src/zed.py")], next_cursor: null },
  };

  it("expands with the keyboard, opens a file and discloses partial listing", async () => {
    mount(pages);
    const tree = await screen.findByRole("tree", { name: "Files" });
    expect(
      screen.getByText(/3 files listed; more are available/),
    ).toBeInTheDocument();
    const src = within(tree).getByRole("treeitem", { name: "src" });
    src.focus();
    fireEvent.keyDown(tree, { key: "ArrowRight" });
    expect(src).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(tree, { key: "ArrowDown" });
    expect(document.activeElement).toHaveAccessibleName("util");
    fireEvent.keyDown(tree, { key: "ArrowDown" });
    expect(document.activeElement).toHaveAccessibleName("app.py");
    fireEvent.keyDown(tree, { key: "Enter" });
    expect(useOpenFiles.getState().activeId).toBe("1");
    expect(window.location.hash).toBe("#source-title");
    expect(screen.getByRole("treeitem", { name: "app.py" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("collapses with ArrowLeft, jumps with Home/End and typeahead", async () => {
    mount(pages);
    const tree = await screen.findByRole("tree");
    const src = within(tree).getByRole("treeitem", { name: "src" });
    src.focus();
    fireEvent.keyDown(tree, { key: "ArrowRight" });
    fireEvent.keyDown(tree, { key: "ArrowLeft" });
    expect(src).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(tree, { key: "End" });
    expect(document.activeElement).toHaveAccessibleName("README.md");
    fireEvent.keyDown(tree, { key: "Home" });
    expect(document.activeElement).toHaveAccessibleName("src");
    fireEvent.keyDown(tree, { key: "r" });
    expect(document.activeElement).toHaveAccessibleName("README.md");
  });

  it("marks ineligible files and loads more on request", async () => {
    mount(pages);
    await screen.findByRole("tree");
    expect(screen.getByRole("treeitem", { name: "README.md" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Load more files" }));
    await waitFor(() =>
      expect(screen.getByText(/4 files listed\./)).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: "Load more files" }),
    ).not.toBeInTheDocument();
  });

  it("reports a listing failure with a retry", async () => {
    vi.spyOn(api, "inventory").mockRejectedValue(new Error("down"));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <FileTree repositoryId="r" snapshotId="s" />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Files could not be listed",
    );
  });
});

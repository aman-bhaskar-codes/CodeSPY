import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { filterCommands, fuzzyScore, type Command } from "./commands";
import { CommandPalette } from "./CommandPalette";
import { WorkspaceShell } from "./WorkspaceShell";
import {
  PANEL_DEFAULT,
  SIDEBAR_DEFAULT,
  useWorkbenchLayout,
} from "./useWorkbenchLayout";

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState({}, "", "/");
  useWorkbenchLayout.setState({
    sidebarVisible: true,
    sidebarWidth: SIDEBAR_DEFAULT,
    panelVisible: false,
    panelHeight: PANEL_DEFAULT,
    panelTab: "problems",
  });
});
afterEach(cleanup);

const cmd = (id: string, title: string, run = () => {}): Command => ({
  id,
  title,
  run,
});

describe("fuzzy matching", () => {
  it("matches subsequences and ranks contiguous word starts first", () => {
    expect(fuzzyScore("tgl", "Toggle panel")).not.toBeNull();
    expect(fuzzyScore("xyz", "Toggle panel")).toBeNull();
    const list = [
      cmd("a", "Go to Files"),
      cmd("b", "View: Toggle explorer"),
      cmd("c", "Toggle theme"),
    ];
    expect(filterCommands(list, "tog").map((c) => c.id)).toEqual(["c", "b"]);
    expect(filterCommands(list, "").map((c) => c.id)).toEqual(["a", "b", "c"]);
  });
});

describe("palette", () => {
  it("filters, moves with arrows and runs the chosen command", () => {
    const first = vi.fn();
    const second = vi.fn();
    const onClose = vi.fn();
    render(
      <CommandPalette
        open
        onClose={onClose}
        commands={[
          cmd("one", "Go to Files", first),
          cmd("two", "Go to Findings", second),
        ]}
      />,
    );
    const box = screen.getByRole("combobox", { name: "Type a command" });
    expect(screen.getAllByRole("option")).toHaveLength(2);
    fireEvent.change(box, { target: { value: "find" } });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Go to Findings",
    ]);
    fireEvent.change(box, { target: { value: "go" } });
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(box).toHaveAttribute(
      "aria-activedescendant",
      screen.getAllByRole("option")[1]!.id,
    );
    fireEvent.keyDown(box, { key: "Enter" });
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
  it("says when nothing matches", () => {
    render(
      <CommandPalette
        open
        onClose={() => {}}
        commands={[cmd("one", "Go to Files")]}
      />,
    );
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("No matching commands")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0 commands");
  });
});

describe("palette in the shell", () => {
  function shell() {
    return render(
      <WorkspaceShell
        sessionState="available"
        destinations={[
          { id: "workspace-home", label: "Overview", enabled: true },
          { id: "inventory-title", label: "Files", enabled: true },
          { id: "findings-title", label: "Findings", enabled: false },
        ]}
        commands={[cmd("extra", "Retry session")]}
      >
        <h1 id="workspace-home">Home</h1>
      </WorkspaceShell>,
    );
  }
  it("opens with Ctrl+Shift+P and navigates only to enabled views", () => {
    shell();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "P", ctrlKey: true, shiftKey: true });
    const titles = screen.getAllByRole("option").map((o) => o.textContent);
    expect(titles).toContain("Go to Files");
    expect(titles).not.toContain("Go to Findings");
    expect(titles).toContain("Retry session");
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "files" },
    });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(window.location.hash).toBe("#inventory-title");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
  it("runs layout and theme commands and also opens from the status bar", () => {
    shell();
    fireEvent.click(screen.getByRole("button", { name: "Commands" }));
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "toggle panel" },
    });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(useWorkbenchLayout.getState().panelVisible).toBe(true);
    fireEvent.keyDown(window, { key: "F1" });
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "theme" },
    });
    const before = document.documentElement.dataset.theme;
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(document.documentElement.dataset.theme).not.toBe(before);
  });
  it("does not open from inside another dialog", () => {
    shell();
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const input = document.createElement("input");
    dialog.appendChild(input);
    document.body.appendChild(dialog);
    fireEvent.keyDown(input, { key: "P", ctrlKey: true, shiftKey: true });
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    dialog.remove();
  });
});

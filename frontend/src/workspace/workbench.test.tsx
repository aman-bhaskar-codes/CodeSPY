import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceShell } from "./WorkspaceShell";
import { Splitter } from "./Splitter";
import {
  SIDEBAR_DEFAULT,
  SIDEBAR_MAX,
  SIDEBAR_MIN,
  clampSidebar,
  useWorkbenchLayout,
} from "./useWorkbenchLayout";

beforeEach(() => {
  window.localStorage.clear();
  useWorkbenchLayout.setState({
    sidebarVisible: true,
    sidebarWidth: SIDEBAR_DEFAULT,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const destinations = [
  { id: "workspace-home", label: "Overview", enabled: true },
  { id: "inventory-title", label: "Files", enabled: true },
];

function shell(children = <h1 id="workspace-home">Home</h1>) {
  return render(
    <WorkspaceShell
      sessionState="available"
      destinations={destinations}
      repositoryLabel="demo"
      snapshotId="12345678-aaaa"
    >
      {children}
    </WorkspaceShell>,
  );
}

describe("layout state", () => {
  it("clamps widths and ignores junk", () => {
    expect(clampSidebar(10)).toBe(SIDEBAR_MIN);
    expect(clampSidebar(9999)).toBe(SIDEBAR_MAX);
    expect(clampSidebar(Number.NaN)).toBe(SIDEBAR_DEFAULT);
  });
  it("remembers the width and survives blocked storage", () => {
    useWorkbenchLayout.getState().setSidebarWidth(300);
    expect(
      JSON.parse(window.localStorage.getItem("codespy.workbench.layout")!),
    ).toMatchObject({
      sidebarWidth: 300,
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() =>
      useWorkbenchLayout.getState().setSidebarWidth(320),
    ).not.toThrow();
    expect(useWorkbenchLayout.getState().sidebarWidth).toBe(320);
  });
});

describe("splitter", () => {
  it("resizes with the keyboard and resets", () => {
    const onChange = vi.fn();
    const onReset = vi.fn();
    render(
      <Splitter
        label="Resize explorer"
        value={240}
        min={160}
        max={480}
        step={16}
        onChange={onChange}
        onReset={onReset}
      />,
    );
    const bar = screen.getByRole("separator", { name: "Resize explorer" });
    expect(bar).toHaveAttribute("aria-valuenow", "240");
    expect(bar).toHaveAttribute("aria-valuemin", "160");
    fireEvent.keyDown(bar, { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith(256);
    fireEvent.keyDown(bar, { key: "ArrowLeft", shiftKey: true });
    expect(onChange).toHaveBeenLastCalledWith(176);
    fireEvent.keyDown(bar, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith(160);
    fireEvent.keyDown(bar, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith(480);
    fireEvent.keyDown(bar, { key: "Enter" });
    expect(onReset).toHaveBeenCalledTimes(1);
    fireEvent.doubleClick(bar);
    expect(onReset).toHaveBeenCalledTimes(2);
  });
  it("drags with the pointer", () => {
    const onChange = vi.fn();
    render(
      <Splitter
        label="Resize explorer"
        value={240}
        min={160}
        max={480}
        step={16}
        onChange={onChange}
        onReset={() => {}}
      />,
    );
    const bar = screen.getByRole("separator");
    fireEvent.pointerDown(bar, { clientX: 100, pointerId: 1 });
    fireEvent.pointerMove(bar, { clientX: 140 });
    expect(onChange).toHaveBeenLastCalledWith(280);
    fireEvent.pointerUp(bar, { pointerId: 1 });
    fireEvent.pointerMove(bar, { clientX: 400 });
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});

describe("workbench shortcuts", () => {
  it("toggles the explorer with Ctrl+B but not while typing", () => {
    shell(<input aria-label="Notes" />);
    const root = document.querySelector("[data-workbench]")!;
    expect(root).toHaveAttribute("data-sidebar", "open");
    fireEvent.keyDown(window, { key: "b", ctrlKey: true });
    expect(root).toHaveAttribute("data-sidebar", "closed");
    fireEvent.keyDown(window, { key: "b", ctrlKey: true });
    expect(root).toHaveAttribute("data-sidebar", "open");
    const input = screen.getByLabelText("Notes");
    input.focus();
    fireEvent.keyDown(input, { key: "b", ctrlKey: true });
    expect(root).toHaveAttribute("data-sidebar", "open");
  });
  it("cycles workbench parts with F6 and Shift+F6", () => {
    shell();
    const main = screen.getByRole("main");
    act(() => main.focus());
    fireEvent.keyDown(window, { key: "F6" });
    expect(document.activeElement).toBe(screen.getByRole("contentinfo"));
    fireEvent.keyDown(window, { key: "F6" });
    expect(document.activeElement).toBe(
      screen.getByRole("link", { name: "Overview" }),
    );
    fireEvent.keyDown(window, { key: "F6", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole("contentinfo"));
  });
  it("skips the closed explorer when cycling", () => {
    useWorkbenchLayout.setState({ sidebarVisible: false });
    shell();
    act(() => screen.getByRole("link", { name: "Overview" }).focus());
    fireEvent.keyDown(window, { key: "F6" });
    expect(document.activeElement).toBe(screen.getByRole("main"));
  });
});

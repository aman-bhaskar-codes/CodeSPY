import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BottomPanel } from "./BottomPanel";
import { Splitter } from "./Splitter";
import { WorkspaceShell } from "./WorkspaceShell";
import {
  PANEL_DEFAULT,
  PANEL_MAX,
  PANEL_MIN,
  SIDEBAR_DEFAULT,
  clampPanel,
  useWorkbenchLayout,
} from "./useWorkbenchLayout";

beforeEach(() => {
  window.localStorage.clear();
  useWorkbenchLayout.setState({
    sidebarVisible: true,
    sidebarWidth: SIDEBAR_DEFAULT,
    panelVisible: false,
    panelHeight: PANEL_DEFAULT,
    panelTab: "problems",
  });
});
afterEach(cleanup);

describe("panel layout state", () => {
  it("clamps, persists and opens a chosen tab", () => {
    expect(clampPanel(1)).toBe(PANEL_MIN);
    expect(clampPanel(9999)).toBe(PANEL_MAX);
    expect(clampPanel(Number.NaN)).toBe(PANEL_DEFAULT);
    const state = useWorkbenchLayout.getState();
    state.showPanel("output");
    expect(useWorkbenchLayout.getState()).toMatchObject({
      panelVisible: true,
      panelTab: "output",
    });
    expect(
      JSON.parse(window.localStorage.getItem("codespy.workbench.layout")!),
    ).toMatchObject({
      panelVisible: true,
      panelTab: "output",
    });
  });
});

describe("horizontal splitter", () => {
  it("grows when dragged up and with ArrowUp", () => {
    const onChange = vi.fn();
    render(
      <Splitter
        orientation="horizontal"
        label="Resize panel"
        value={180}
        min={80}
        max={480}
        step={16}
        onChange={onChange}
        onReset={() => {}}
      />,
    );
    const bar = screen.getByRole("separator", { name: "Resize panel" });
    expect(bar).toHaveAttribute("aria-orientation", "horizontal");
    fireEvent.keyDown(bar, { key: "ArrowUp" });
    expect(onChange).toHaveBeenLastCalledWith(196);
    fireEvent.keyDown(bar, { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith(164);
    fireEvent.pointerDown(bar, { clientY: 300, pointerId: 1 });
    fireEvent.pointerMove(bar, { clientY: 260 });
    expect(onChange).toHaveBeenLastCalledWith(220);
  });
});

describe("BottomPanel", () => {
  it("switches tabs by keyboard and lists problems and output", () => {
    const onTab = vi.fn();
    const { rerender } = render(
      <BottomPanel
        height={180}
        tab="problems"
        problems={["Upload failed"]}
        output={[{ at: "10:00:00", text: "Job queued" }]}
        onTab={onTab}
        onHeight={() => {}}
        onResetHeight={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByRole("list", { name: "Problems" })).toHaveTextContent(
      "Upload failed",
    );
    expect(screen.getByRole("tab", { name: /Problems/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.keyDown(screen.getByRole("tablist", { name: "Panel views" }), {
      key: "ArrowRight",
    });
    expect(onTab).toHaveBeenCalledWith("output");
    rerender(
      <BottomPanel
        height={180}
        tab="output"
        problems={[]}
        output={[{ at: "10:00:00", text: "Job queued" }]}
        onTab={onTab}
        onHeight={() => {}}
        onResetHeight={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByRole("list", { name: "Output" })).toHaveTextContent(
      "Job queued",
    );
  });
  it("says so when there is nothing to show", () => {
    render(
      <BottomPanel
        height={180}
        tab="problems"
        problems={[]}
        output={[]}
        onTab={() => {}}
        onHeight={() => {}}
        onResetHeight={() => {}}
        onClose={() => {}}
      />,
    );
    expect(
      screen.getByText("No problems have been detected."),
    ).toBeInTheDocument();
  });
});

describe("panel in the shell", () => {
  function shell(children: React.ReactNode) {
    return render(
      <WorkspaceShell
        sessionState="available"
        destinations={[
          { id: "workspace-home", label: "Overview", enabled: true },
        ]}
      >
        {children}
      </WorkspaceShell>,
    );
  }
  it("toggles with Ctrl+J and the status bar, and is skipped by F6 while closed", () => {
    shell(<h1 id="workspace-home">Home</h1>);
    expect(
      screen.queryByRole("region", { name: "Panel" }),
    ).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "j", ctrlKey: true });
    expect(screen.getByRole("region", { name: "Panel" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close panel" }));
    expect(
      screen.queryByRole("region", { name: "Panel" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Problems: 0/ }));
    expect(screen.getByRole("region", { name: "Panel" })).toBeInTheDocument();
  });
  it("collects alerts from the editor into Problems and status messages into Output", async () => {
    const { rerender } = shell(
      <p role="alert">Sign-out could not be confirmed.</p>,
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Problems: 1" }),
      ).toBeInTheDocument(),
    );
    act(() => useWorkbenchLayout.getState().showPanel("problems"));
    const panel = screen.getByRole("region", { name: "Panel" });
    expect(
      within(panel).getByRole("list", { name: "Problems" }),
    ).toHaveTextContent("Sign-out could not be confirmed.");
    rerender(
      <WorkspaceShell
        sessionState="available"
        destinations={[
          { id: "workspace-home", label: "Overview", enabled: true },
        ]}
      >
        <p role="status">Job queued</p>
      </WorkspaceShell>,
    );
    act(() => useWorkbenchLayout.getState().showPanel("output"));
    await waitFor(() =>
      expect(
        within(screen.getByRole("region", { name: "Panel" })).getByRole(
          "list",
          { name: "Output" },
        ),
      ).toHaveTextContent("Job queued"),
    );
    // The panel's own text is outside the editor, so it never feeds back into Problems.
    expect(
      screen.getByRole("button", { name: "Problems: 0" }),
    ).toBeInTheDocument();
  });
});

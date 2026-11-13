import { create } from "zustand";

export const SIDEBAR_MIN = 160;
export const SIDEBAR_MAX = 480;
export const SIDEBAR_DEFAULT = 240;
export const SIDEBAR_STEP = 16;
export const PANEL_MIN = 80;
export const PANEL_MAX = 480;
export const PANEL_DEFAULT = 180;
export type PanelTab = "problems" | "output";
const KEY = "codespy.workbench.layout";

export interface WorkbenchLayout {
  sidebarVisible: boolean;
  sidebarWidth: number;
  panelVisible: boolean;
  panelHeight: number;
  panelTab: PanelTab;
  setPanelHeight: (height: number) => void;
  resetPanelHeight: () => void;
  togglePanel: () => void;
  showPanel: (tab: PanelTab) => void;
  setSidebarWidth: (width: number) => void;
  resetSidebarWidth: () => void;
  toggleSidebar: () => void;
}

export function clampSidebar(width: number): number {
  if (!Number.isFinite(width)) return SIDEBAR_DEFAULT;
  return Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, Math.round(width)));
}

export function clampPanel(height: number): number {
  if (!Number.isFinite(height)) return PANEL_DEFAULT;
  return Math.min(PANEL_MAX, Math.max(PANEL_MIN, Math.round(height)));
}

type Saved = Pick<
  WorkbenchLayout,
  | "sidebarVisible"
  | "sidebarWidth"
  | "panelVisible"
  | "panelHeight"
  | "panelTab"
>;

function load(): Saved {
  const fallback: Saved = {
    sidebarVisible: true,
    sidebarWidth: SIDEBAR_DEFAULT,
    panelVisible: false,
    panelHeight: PANEL_DEFAULT,
    panelTab: "problems",
  };
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return {
      sidebarVisible:
        typeof parsed.sidebarVisible === "boolean"
          ? parsed.sidebarVisible
          : true,
      sidebarWidth: clampSidebar(Number(parsed.sidebarWidth)),
      panelVisible:
        typeof parsed.panelVisible === "boolean" ? parsed.panelVisible : false,
      panelHeight: clampPanel(Number(parsed.panelHeight ?? PANEL_DEFAULT)),
      panelTab: parsed.panelTab === "output" ? "output" : "problems",
    };
  } catch {
    return fallback; // storage can be blocked or hold anything
  }
}

function save(state: Saved) {
  try {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        sidebarVisible: state.sidebarVisible,
        sidebarWidth: state.sidebarWidth,
        panelVisible: state.panelVisible,
        panelHeight: state.panelHeight,
        panelTab: state.panelTab,
      }),
    );
  } catch {
    /* the layout simply is not remembered */
  }
}

export const useWorkbenchLayout = create<WorkbenchLayout>((set, get) => ({
  ...load(),
  setSidebarWidth: (width) => {
    set({ sidebarWidth: clampSidebar(width) });
    save(get());
  },
  resetSidebarWidth: () => {
    set({ sidebarWidth: SIDEBAR_DEFAULT });
    save(get());
  },
  setPanelHeight: (height) => {
    set({ panelHeight: clampPanel(height) });
    save(get());
  },
  resetPanelHeight: () => {
    set({ panelHeight: PANEL_DEFAULT });
    save(get());
  },
  togglePanel: () => {
    set({ panelVisible: !get().panelVisible });
    save(get());
  },
  showPanel: (tab) => {
    set({ panelVisible: true, panelTab: tab });
    save(get());
  },
  toggleSidebar: () => {
    set({ sidebarVisible: !get().sidebarVisible });
    save(get());
  },
}));

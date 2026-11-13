import { create } from "zustand";

export const SIDEBAR_MIN = 160;
export const SIDEBAR_MAX = 480;
export const SIDEBAR_DEFAULT = 240;
export const SIDEBAR_STEP = 16;
const KEY = "codespy.workbench.layout";

export interface WorkbenchLayout {
  sidebarVisible: boolean;
  sidebarWidth: number;
  setSidebarWidth: (width: number) => void;
  resetSidebarWidth: () => void;
  toggleSidebar: () => void;
}

export function clampSidebar(width: number): number {
  if (!Number.isFinite(width)) return SIDEBAR_DEFAULT;
  return Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, Math.round(width)));
}

function load(): Pick<WorkbenchLayout, "sidebarVisible" | "sidebarWidth"> {
  const fallback = { sidebarVisible: true, sidebarWidth: SIDEBAR_DEFAULT };
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
    };
  } catch {
    return fallback; // storage can be blocked or hold anything
  }
}

function save(state: Pick<WorkbenchLayout, "sidebarVisible" | "sidebarWidth">) {
  try {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        sidebarVisible: state.sidebarVisible,
        sidebarWidth: state.sidebarWidth,
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
  toggleSidebar: () => {
    set({ sidebarVisible: !get().sidebarVisible });
    save(get());
  },
}));

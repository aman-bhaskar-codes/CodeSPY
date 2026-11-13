import { create } from "zustand";

export interface OpenFile {
  fileId: string;
  path: string;
}

interface OpenFiles {
  files: OpenFile[];
  activeId: string | null;
  open: (file: OpenFile) => void;
  close: (fileId: string) => void;
  reset: () => void;
}

export const useOpenFiles = create<OpenFiles>((set, get) => ({
  files: [],
  activeId: null,
  open: (file) => {
    const known = get().files.some((f) => f.fileId === file.fileId);
    set({
      files: known ? get().files : [...get().files, file],
      activeId: file.fileId,
    });
  },
  close: (fileId) => {
    const files = get().files.filter((f) => f.fileId !== fileId);
    const closing = get().activeId === fileId;
    const index = get().files.findIndex((f) => f.fileId === fileId);
    set({
      files,
      activeId: closing
        ? (files[Math.min(index, files.length - 1)]?.fileId ?? null)
        : get().activeId,
    });
  },
  reset: () => set({ files: [], activeId: null }),
}));

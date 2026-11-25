export interface TreeFile {
  id: string;
  path: string;
  eligible: boolean;
}

export interface TreeRow {
  key: string; // directory path (with trailing "/") or file id
  kind: "dir" | "file";
  name: string;
  path: string;
  depth: number;
  fileId?: string;
  eligible?: boolean;
}

interface Dir {
  name: string;
  path: string;
  dirs: Map<string, Dir>;
  files: TreeFile[];
}

function insert(root: Dir, file: TreeFile) {
  const parts = file.path.split("/");
  let dir = root;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const name = parts[i]!;
    let next = dir.dirs.get(name);
    if (!next) {
      next = {
        name,
        path: parts.slice(0, i + 1).join("/") + "/",
        dirs: new Map(),
        files: [],
      };
      dir.dirs.set(name, next);
    }
    dir = next;
  }
  dir.files.push(file);
}

/** Directories first, then files, each sorted by name; only the children of expanded directories are listed. */
export function visibleRows(
  files: readonly TreeFile[],
  expanded: ReadonlySet<string>,
): TreeRow[] {
  const root: Dir = { name: "", path: "", dirs: new Map(), files: [] };
  for (const file of files) insert(root, file);
  const rows: TreeRow[] = [];
  const walk = (dir: Dir, depth: number) => {
    for (const child of [...dir.dirs.values()].sort((a, b) =>
      a.name.localeCompare(b.name),
    )) {
      rows.push({
        key: child.path,
        kind: "dir",
        name: child.name,
        path: child.path,
        depth,
      });
      if (expanded.has(child.path)) walk(child, depth + 1);
    }
    for (const file of [...dir.files].sort((a, b) =>
      a.path.localeCompare(b.path),
    )) {
      rows.push({
        key: file.id,
        kind: "file",
        name: file.path.slice(file.path.lastIndexOf("/") + 1),
        path: file.path,
        depth,
        fileId: file.id,
        eligible: file.eligible,
      });
    }
  };
  walk(root, 0);
  return rows;
}

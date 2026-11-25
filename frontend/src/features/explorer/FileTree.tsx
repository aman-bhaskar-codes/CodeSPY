import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  File as FileIcon,
  Folder,
} from "lucide-react";
import { api } from "../../api/client";
import { useOpenFiles } from "../source/useOpenFiles";
import { visibleRows, type TreeFile } from "./tree";
import styles from "./FileTree.module.css";

const MAX_PAGES = 40; // 2,000 files: beyond this the tree says so instead of paging forever

interface Props {
  repositoryId: string;
  snapshotId: string;
}

export function FileTree({ repositoryId, snapshotId }: Props) {
  const query = useInfiniteQuery({
    queryKey: ["explorer-inventory", repositoryId, snapshotId],
    queryFn: ({ pageParam, signal }) =>
      api.inventory(snapshotId, pageParam, signal, repositoryId),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    maxPages: MAX_PAGES,
  });
  const open = useOpenFiles((s) => s.open);
  const activeId = useOpenFiles((s) => s.activeId);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const refs = useRef(new Map<string, HTMLLIElement>());
  const typed = useRef({ text: "", at: 0 });

  const files: TreeFile[] = useMemo(
    () =>
      (query.data?.pages ?? []).flatMap((p) =>
        p.items.map((i) => ({ id: i.id, path: i.path, eligible: i.eligible })),
      ),
    [query.data],
  );
  const rows = useMemo(() => visibleRows(files, expanded), [files, expanded]);
  const current = rows.find((r) => r.key === focusKey) ?? rows[0];

  function focusRow(key: string) {
    setFocusKey(key);
    refs.current.get(key)?.focus();
  }
  function toggle(path: string, force?: boolean) {
    const next = new Set(expanded);
    const want = force ?? !next.has(path);
    if (want) next.add(path);
    else next.delete(path);
    setExpanded(next);
  }
  function activate(row: (typeof rows)[number]) {
    if (row.kind === "dir") toggle(row.path);
    else if (row.fileId) {
      open({ fileId: row.fileId, path: row.path });
      window.location.assign("#source-title");
    }
  }
  function onKeyDown(event: KeyboardEvent<HTMLUListElement>) {
    if (!current) return;
    const index = rows.findIndex((r) => r.key === current.key);
    const move = (i: number) => {
      const row = rows[Math.max(0, Math.min(rows.length - 1, i))];
      if (row) focusRow(row.key);
    };
    switch (event.key) {
      case "ArrowDown":
        move(index + 1);
        break;
      case "ArrowUp":
        move(index - 1);
        break;
      case "Home":
        move(0);
        break;
      case "End":
        move(rows.length - 1);
        break;
      case "ArrowRight":
        if (current.kind === "dir") {
          if (!expanded.has(current.path)) toggle(current.path, true);
          else move(index + 1);
        }
        break;
      case "ArrowLeft":
        if (current.kind === "dir" && expanded.has(current.path)) {
          toggle(current.path, false);
        } else {
          const parent = [...rows.slice(0, index)]
            .reverse()
            .find((r) => r.kind === "dir" && r.depth < current.depth);
          if (parent) focusRow(parent.key);
        }
        break;
      case "Enter":
      case " ":
        activate(current);
        break;
      default: {
        if (event.key.length !== 1 || event.ctrlKey || event.metaKey) return;
        const now = event.timeStamp;
        typed.current.text =
          now - typed.current.at > 700
            ? event.key.toLowerCase()
            : typed.current.text + event.key.toLowerCase();
        typed.current.at = now;
        const hit = [
          ...rows.slice(index + 1),
          ...rows.slice(0, index + 1),
        ].find((r) => r.name.toLowerCase().startsWith(typed.current.text));
        if (hit) focusRow(hit.key);
        return;
      }
    }
    event.preventDefault();
  }

  if (query.isPending) return <p role="status">Loading files…</p>;
  if (query.isError) {
    return (
      <p role="alert">
        Files could not be listed.{" "}
        <button onClick={() => void query.refetch()}>Retry</button>
      </p>
    );
  }
  if (files.length === 0) return <p>No files were admitted.</p>;
  const capped = (query.data?.pages.length ?? 0) >= MAX_PAGES;
  return (
    <div className={styles.wrap}>
      <ul
        role="tree"
        aria-label="Files"
        className={styles.tree}
        onKeyDown={onKeyDown}
      >
        {rows.map((row) => {
          const isDir = row.kind === "dir";
          const isOpen = isDir && expanded.has(row.path);
          return (
            <li
              key={row.key}
              ref={(el) => {
                if (el) refs.current.set(row.key, el);
                else refs.current.delete(row.key);
              }}
              role="treeitem"
              aria-level={row.depth + 1}
              aria-expanded={isDir ? isOpen : undefined}
              aria-selected={!isDir && row.fileId === activeId}
              aria-disabled={
                !isDir && row.eligible === false ? true : undefined
              }
              tabIndex={current && row.key === current.key ? 0 : -1}
              className={styles.row}
              style={{ paddingLeft: 8 + row.depth * 12 }}
              title={row.path}
              onFocus={() => setFocusKey(row.key)}
              onClick={() => activate(row)}
            >
              {isDir ? (
                isOpen ? (
                  <ChevronDown size={14} aria-hidden="true" />
                ) : (
                  <ChevronRight size={14} aria-hidden="true" />
                )
              ) : (
                <span className={styles.gap} aria-hidden="true" />
              )}
              {isDir ? (
                <Folder size={14} aria-hidden="true" />
              ) : (
                <FileIcon size={14} aria-hidden="true" />
              )}
              <span className={styles.name}>{row.name}</span>
            </li>
          );
        })}
      </ul>
      <p className={styles.note} role="status">
        {files.length} files listed
        {query.hasNextPage
          ? capped
            ? `; the first ${files.length} only`
            : "; more are available"
          : ""}
        .
      </p>
      {query.hasNextPage && !capped && (
        <button
          className={styles.more}
          disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          {query.isFetchingNextPage ? "Loading…" : "Load more files"}
        </button>
      )}
    </div>
  );
}

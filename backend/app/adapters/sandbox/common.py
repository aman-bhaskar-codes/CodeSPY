"""Behaviour every sandbox implementation shares, so the fake and the real ones cannot disagree."""

from collections.abc import Mapping

from app.contracts.workspace import FileEntryV1, FileListingV1, FileSliceV1
from app.ports.sandbox import (
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    MAX_READ_LINES,
    NotATextFile,
    PathRejected,
    content_hash,
    normalize_workspace_path,
)


def listing(files: Mapping[str, tuple[int, str]], prefix: str, limit: int) -> FileListingV1:
    """Entries under ``prefix`` (a directory, not a string prefix), sorted, bounded.

    ``files`` maps each path to its size and content hash.
    """
    root = normalize_workspace_path(prefix, allow_root=True)
    limit = max(1, min(limit, MAX_LIST_ENTRIES))
    names = sorted(p for p in files if root == "." or p == root or p.startswith(root + "/"))
    entries = tuple(
        FileEntryV1(path=p, size=files[p][0], file_hash=files[p][1]) for p in names[:limit]
    )
    return FileListingV1(entries=entries, truncated=len(names) > limit)


def text_slice(rel: str, data: bytes, start_line: int, end_line: int | None) -> FileSliceV1:
    """A bounded, line-numbered view of a UTF-8 text file."""
    if start_line < 1 or (end_line is not None and end_line < start_line):
        raise PathRejected("line range is invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise NotATextFile("file is not UTF-8 text") from None
    if "\x00" in text:
        raise NotATextFile("file is not text")
    lines = text.splitlines(keepends=True)
    total = len(lines)
    last = total if end_line is None else min(end_line, total)
    truncated = False
    if last - start_line + 1 > MAX_READ_LINES:
        last, truncated = start_line + MAX_READ_LINES - 1, True
    chosen = lines[start_line - 1 : last] if start_line <= total else []
    content = "".join(chosen)
    if len(content.encode()) > MAX_READ_BYTES:
        content = content.encode()[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        truncated = True
        last = start_line + content.count("\n") - 1
    return FileSliceV1(
        path=rel,
        file_hash=content_hash(data),
        start_line=start_line,
        end_line=max(last, 0) if chosen else 0,
        total_lines=total,
        content=content,
        truncated=truncated,
    )

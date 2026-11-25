"""The execution sandbox port: the only boundary where uploaded source is edited or run.

Every implementation must pass ``tests/agent/sandbox/contract.py``. Rules of the port:

* A sandbox belongs to one organisation and one session. ``acquire`` is idempotent for a live
  sandbox of that session (so a restarted worker re-attaches) and returns a new, empty one after
  ``release``. A sandbox is never reused across sessions.
* A handle is not authority. Every call checks it, and an unknown, foreign or released handle is
  ``SandboxGone``.
* Paths are workspace-relative and canonical (see ``normalize_workspace_path``). Nothing outside
  the workspace is reachable, and ``.git`` is not addressable through file calls.
* ``write_files`` and ``restore`` are atomic and serialised per sandbox: a rejected call changes
  nothing, and each successful call advances the revision by exactly one.
* ``file_hashes`` maps every file of the workspace (at most 100,000) to its SHA-256, for comparing
  against a snapshot.
* ``read_bytes`` returns a whole regular file (at most 8 MiB) for tools that edit or search it;
  it is never sent to a model without redaction.
* Content is addressed by SHA-256. A change names the hash it expects, so a stale edit is a
  conflict, never a silent overwrite.
* ``exec`` runs argv without a shell, with no network unless the spec allows a proxy, and ends with
  exactly one ``exit`` chunk. A command is never re-run by the port. A command that changes the
  workspace advances the revision by one. Processes a command starts do not outlive it.
* ``suspend`` keeps the workspace; the next call resumes it. A sandbox that cannot suspend keeps
  running and says so in its capabilities.
"""

import hashlib
import unicodedata
from collections.abc import AsyncIterator, Mapping
from typing import Protocol
from uuid import UUID

from app.contracts.workspace import (
    CheckpointRefV1,
    ExecChunkV1,
    ExecRequestV1,
    FileChangeV1,
    FileListingV1,
    FileSliceV1,
    SandboxCapabilitiesV1,
    SandboxHandleV1,
    SandboxSpecV1,
    SnapshotRefV1,
    WorkspaceRevisionV1,
)

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CHANGES_PER_CALL = 200
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_FILES = 100_000
MAX_READ_LINES = 2000
MAX_READ_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 2000
MAX_EXEC_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_COMPONENT_BYTES = 255
MAX_PATH_DEPTH = 64


class SandboxError(Exception):
    """Base of every sandbox failure. ``code`` is stable and safe to show."""

    code = "sandbox_error"


class SandboxGone(SandboxError):  # noqa: N818 -- public contract name
    code = "sandbox_gone"


class SandboxUnavailable(SandboxError):  # noqa: N818
    """The runtime behind the sandbox (daemon, image, host) cannot serve the call."""

    code = "sandbox_unavailable"


class SandboxStateError(SandboxError):
    code = "sandbox_state"


class PathRejected(SandboxError):  # noqa: N818
    code = "path_rejected"


class WorkspaceFileNotFound(SandboxError):  # noqa: N818
    code = "file_not_found"


class NotATextFile(SandboxError):  # noqa: N818
    code = "not_text"


class SandboxLimit(SandboxError):  # noqa: N818
    code = "sandbox_limit"


class ExecRejected(SandboxError):  # noqa: N818
    code = "exec_rejected"


class CheckpointUnknown(SandboxError):  # noqa: N818
    code = "checkpoint_unknown"


class WorkspaceConflict(SandboxError):  # noqa: N818
    """A change named a hash the file does not have, or a create found the path taken."""

    code = "workspace_conflict"

    def __init__(self, path: str, expected: str | None, actual: str | None) -> None:
        super().__init__(f"conflict at {path}")
        self.path, self.expected, self.actual = path, expected, actual


def normalize_workspace_path(path: str, *, allow_root: bool = False) -> str:
    """The one canonical form of a workspace path, or ``PathRejected``.

    Accepts a single leading ``./``. Rejects absolute paths, ``..``, empty or ``.`` components,
    backslashes, control characters, trailing slashes, over-long names, and any ``.git`` component.
    ``allow_root`` additionally accepts ``.`` (a working directory).
    """
    if allow_root and path in {".", "./"}:
        return "."
    value = path[2:] if path.startswith("./") else path
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise PathRejected("path is not valid text") from None
    parts = value.split("/")
    if (
        not value
        or len(encoded) > MAX_PATH_BYTES
        or len(parts) > MAX_PATH_DEPTH
        or "\\" in value
        or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
        or any(
            p in {"", ".", ".."} or len(p.encode()) > MAX_COMPONENT_BYTES or p.casefold() == ".git"
            for p in parts
        )
    ):
        raise PathRejected("path is not a canonical workspace-relative path")
    return value


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def tree_digest(file_hashes: Mapping[str, str]) -> str:
    """Hash of a whole workspace: the sorted (path, content hash) pairs."""
    h = hashlib.sha256()
    for path in sorted(file_hashes):
        h.update(path.encode())
        h.update(b"\x00")
        h.update(file_hashes[path].encode())
        h.update(b"\n")
    return h.hexdigest()


class SnapshotFilesPort(Protocol):
    """Where a sandbox gets the immutable files of a snapshot. Implementations never see a path."""

    def files(self, ref: SnapshotRefV1) -> AsyncIterator[tuple[str, bytes]]: ...


class SandboxPort(Protocol):
    @property
    def capabilities(self) -> SandboxCapabilitiesV1: ...

    async def acquire(self, spec: SandboxSpecV1) -> SandboxHandleV1: ...

    async def materialize(
        self, handle: SandboxHandleV1, snapshot: SnapshotRefV1
    ) -> WorkspaceRevisionV1: ...

    async def status(self, handle: SandboxHandleV1) -> WorkspaceRevisionV1: ...

    async def list_files(
        self, handle: SandboxHandleV1, prefix: str = ".", limit: int = MAX_LIST_ENTRIES
    ) -> FileListingV1: ...

    async def read_file(
        self, handle: SandboxHandleV1, path: str, start_line: int = 1, end_line: int | None = None
    ) -> FileSliceV1: ...

    async def read_bytes(self, handle: SandboxHandleV1, path: str) -> bytes: ...

    async def file_hashes(self, handle: SandboxHandleV1) -> Mapping[str, str]: ...

    async def write_files(
        self, handle: SandboxHandleV1, changes: tuple[FileChangeV1, ...]
    ) -> WorkspaceRevisionV1: ...

    def exec(
        self, handle: SandboxHandleV1, request: ExecRequestV1
    ) -> AsyncIterator[ExecChunkV1]: ...

    async def checkpoint(self, handle: SandboxHandleV1, label: str = "") -> CheckpointRefV1: ...

    async def restore(
        self, handle: SandboxHandleV1, checkpoint: CheckpointRefV1
    ) -> WorkspaceRevisionV1: ...

    async def suspend(self, handle: SandboxHandleV1) -> None: ...

    async def release(self, handle: SandboxHandleV1) -> None: ...


def sandbox_key(org_id: UUID, session_id: UUID) -> tuple[UUID, UUID]:
    return org_id, session_id

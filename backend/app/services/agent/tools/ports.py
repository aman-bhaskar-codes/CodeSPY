"""Services the read tools consult besides the sandbox. Each is optional: a tool whose service
is not wired returns ``unavailable`` instead of guessing."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.contracts.workspace import FileChangeV1
from app.services.workspace.changes import Applied


@dataclass(frozen=True)
class SymbolHit:
    path: str
    line: int
    kind: str
    name: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    start_line: int
    end_line: int
    snippet: str
    score: float


class BaseFilesPort(Protocol):
    """The immutable files of the snapshot the session started from."""

    async def hashes(self) -> Mapping[str, str]: ...

    async def read(self, path: str) -> bytes | None: ...


class SymbolIndexPort(Protocol):
    """Definitions found by structural analysis of the base snapshot."""

    async def find(self, name: str, kind: str | None, limit: int) -> list[SymbolHit]: ...


class CodeSearchPort(Protocol):
    """Ranked retrieval over the base snapshot (lexical, dense or hybrid)."""

    async def search(self, query: str, limit: int) -> list[SearchHit]: ...


class WorkspaceEditPort(Protocol):
    """Applies changes to the session's workspace and records them. Idempotent by content."""

    async def apply(
        self,
        changes: tuple[FileChangeV1, ...],
        *,
        request_digest: str | None = None,
        author: str = "agent",
    ) -> Applied:
        """Write the changes, or report them already applied. The digest names the tool request."""
        ...

    async def find_applied(self, request_digest: str) -> Applied | None:
        """The earlier application of this request, if the workspace still has its result."""
        ...

    async def sync(self, *, author: str = "agent") -> Applied | None:
        """Record changes made outside the edit tools (a command wrote files); ``None`` if none."""
        ...

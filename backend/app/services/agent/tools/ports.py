"""Services the read tools consult besides the sandbox. Each is optional: a tool whose service
is not wired returns ``unavailable`` instead of guessing."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


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

"""Path lookups shared by the per-language import resolvers (analyzed files only)."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from posixpath import dirname, normpath

FILE = "file:"
PACKAGE = "pkg:"
EXTERNAL = "ext:"


def parent(path: str) -> str:
    return dirname(path)


def join(base: str, *parts: str) -> str:
    """Normalize a repository-relative join; '' is the root."""
    joined = normpath("/".join(p for p in (base, *parts) if p))
    return "" if joined in {".", ""} else joined


def escapes(base: str, relative: str) -> bool:
    """True when ``relative`` climbs above the repository root from ``base``."""
    depth = len([p for p in base.split("/") if p])
    for part in relative.split("/"):
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        elif part not in {"", "."}:
            depth += 1
    return False


@dataclass
class PathIndex:
    """Lookups over the files that were analyzed; ``inventory`` is every eligible path."""

    paths: frozenset[str]
    inventory: frozenset[str]
    by_basename: dict[str, list[str]] = field(default_factory=dict)
    by_dir: dict[str, list[str]] = field(default_factory=dict)
    by_suffix: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, paths: Iterable[str], inventory: Iterable[str] = ()) -> "PathIndex":
        ordered = sorted(set(paths))
        basenames: dict[str, list[str]] = defaultdict(list)
        dirs: dict[str, list[str]] = defaultdict(list)
        suffixes: dict[str, list[str]] = defaultdict(list)
        for path in ordered:
            parts = path.split("/")
            basenames[parts[-1]].append(path)
            dirs[parent(path)].append(path)
            for i in range(len(parts)):
                suffixes["/".join(parts[i:])].append(path)
        return cls(
            paths=frozenset(ordered),
            inventory=frozenset(inventory) | frozenset(ordered),
            by_basename=dict(basenames),
            by_dir=dict(dirs),
            by_suffix=dict(suffixes),
        )

    def has(self, path: str) -> bool:
        return path in self.paths

    def ending(self, suffix: str) -> list[str]:
        """Files whose path equals ``suffix`` or ends with ``/suffix``."""
        return list(self.by_suffix.get(suffix, ()))

    def closest(self, source: str, candidates: list[str]) -> list[str]:
        """Keep the candidates sharing the longest directory prefix with ``source``."""
        if len(candidates) < 2:
            return candidates
        here = [p for p in parent(source).split("/") if p]

        def shared(path: str) -> int:
            n = 0
            for a, b in zip(here, [p for p in parent(path).split("/") if p], strict=False):
                if a != b:
                    break
                n += 1
            return n

        best = max(shared(c) for c in candidates)
        return [c for c in candidates if shared(c) == best]

    def has_directory(self, directory: str) -> bool:
        return directory in self.by_dir or any(d.startswith(directory + "/") for d in self.by_dir)

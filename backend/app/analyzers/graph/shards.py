"""Partition a snapshot's analyzable files into fact-analysis shards.

One AST-facts analysis reads at most 128 files and 16 MiB and takes at most 16 include prefixes. A
prefix selects a file or a whole directory tree, so a shard is a set of disjoint prefixes. The plan
is a pure function of the file list: the same inventory always gives the same shards, and every
path lands in exactly one shard.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.contracts.structure import normalize_prefixes

MAX_FILES = 128
MAX_PREFIXES = 16
MAX_BYTES = 15 * 1024 * 1024  # under the 16 MiB analysis limit, with margin


@dataclass(frozen=True)
class Shard:
    index: int
    prefixes: tuple[str, ...]  # empty means the whole snapshot (only for a single-shard plan)
    files: int
    bytes: int


@dataclass(frozen=True)
class ShardPlan:
    shards: tuple[Shard, ...]
    total_files: int
    skipped: tuple[str, ...]  # paths no include prefix can name
    digest: str


@dataclass
class _Node:
    files: dict[str, int] = field(default_factory=dict)  # direct files: name -> size
    dirs: dict[str, "_Node"] = field(default_factory=dict)
    count: int = 0
    size: int = 0


def _tree(sizes: Mapping[str, int]) -> _Node:
    root = _Node()
    for path in sorted(sizes):
        parts = path.split("/")
        node = root
        node.count += 1
        node.size += sizes[path]
        for part in parts[:-1]:
            node = node.dirs.setdefault(part, _Node())
            node.count += 1
            node.size += sizes[path]
        node.files[parts[-1]] = sizes[path]
    return root


def _usable(path: str) -> bool:
    try:
        normalize_prefixes((path,))
    except ValueError:
        return False
    return True


def plan_shards(sizes: Mapping[str, int]) -> ShardPlan:
    """``sizes`` maps every analyzable path to its size in bytes."""
    usable = {p: s for p, s in sizes.items() if _usable(p)}
    skipped = tuple(sorted(set(sizes) - set(usable)))
    root = _tree(usable)
    if root.count == 0:
        return ShardPlan((), 0, skipped, hashlib.sha256(b"[]").hexdigest())
    if root.count <= MAX_FILES and root.size <= MAX_BYTES:
        shard = Shard(0, (), root.count, root.size)
        return ShardPlan((shard,), root.count, skipped, _digest((shard,)))

    units: list[tuple[str, int, int]] = []  # (prefix, files, bytes)

    def cover(node: _Node, path: str) -> None:
        if path and node.count <= MAX_FILES and node.size <= MAX_BYTES:
            units.append((path, node.count, node.size))
            return
        for name, size in sorted(node.files.items()):
            units.append((f"{path}/{name}" if path else name, 1, size))
        for name, child in sorted(node.dirs.items()):
            cover(child, f"{path}/{name}" if path else name)

    cover(root, "")
    bins: list[list[tuple[str, int, int]]] = []
    for unit in sorted(units, key=lambda u: (-u[1], -u[2], u[0])):
        for bin_ in bins:
            if (
                len(bin_) < MAX_PREFIXES
                and sum(u[1] for u in bin_) + unit[1] <= MAX_FILES
                and sum(u[2] for u in bin_) + unit[2] <= MAX_BYTES
            ):
                bin_.append(unit)
                break
        else:
            bins.append([unit])
    shards = tuple(
        Shard(
            index=i,
            prefixes=tuple(sorted(u[0] for u in bin_)),
            files=sum(u[1] for u in bin_),
            bytes=sum(u[2] for u in bin_),
        )
        for i, bin_ in enumerate(sorted(bins, key=lambda b: sorted(u[0] for u in b)))
    )
    return ShardPlan(shards, root.count, skipped, _digest(shards))


def _digest(shards: tuple[Shard, ...]) -> str:
    material = "\n".join(f"{s.index}:{'|'.join(s.prefixes)}" for s in shards)
    return hashlib.sha256(material.encode()).hexdigest()


def selects(prefixes: tuple[str, ...], path: str) -> bool:
    """The same prefix rule the source reader applies."""
    return not prefixes or any(path == p or path.startswith(p + "/") for p in prefixes)

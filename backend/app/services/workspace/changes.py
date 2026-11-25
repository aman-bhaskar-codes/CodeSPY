"""A set of file changes as a value: its identity, its result, and whether it was already applied.

The post-image of a change set (the hash every touched path has afterwards) follows from the
changes alone. That makes applying one idempotent without a log: if the workspace already has the
post-image, the changes were applied, whoever applied them and whether or not it was recorded.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.contracts.workspace import FileChangeV1
from app.ports.sandbox import content_hash


def change_digest(changes: Sequence[FileChangeV1]) -> str:
    """Identity of a change set: the same edits to the same base content always hash the same."""
    items = [
        {
            "op": c.op,
            "path": c.path,
            "expected": c.expected_hash,
            "content": content_hash(c.content) if c.content is not None else None,
            "to": c.move_to,
        }
        for c in sorted(changes, key=lambda c: (c.path, c.op))
    ]
    return hashlib.sha256(
        json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def post_image(changes: Sequence[FileChangeV1]) -> dict[str, str | None]:
    """The hash each touched path has once the changes are applied (``None`` means absent)."""
    image: dict[str, str | None] = {}
    for c in changes:
        if c.op == "write":
            assert c.content is not None
            image[c.path] = content_hash(c.content)
        elif c.op == "delete":
            image[c.path] = None
        else:
            assert c.move_to is not None
            image[c.path] = None
            image[c.move_to] = c.expected_hash
    return image


def already_applied(current: Mapping[str, str], changes: Sequence[FileChangeV1]) -> bool:
    return all(current.get(path) == want for path, want in post_image(changes).items())


def changes_json(
    changes: Sequence[FileChangeV1], before: Mapping[str, str]
) -> list[dict[str, Any]]:
    """What a revision records: for each change the hashes before and after, never the content."""
    image = post_image(changes)
    return [
        {
            "op": c.op,
            "path": c.path,
            "before_hash": before.get(c.path),
            "after_hash": image[c.path],
            **({"move_to": c.move_to} if c.move_to else {}),
        }
        for c in changes
    ]


def touched_paths(changes: Sequence[FileChangeV1]) -> tuple[str, ...]:
    paths: list[str] = []
    for c in changes:
        paths.append(c.path)
        if c.move_to:
            paths.append(c.move_to)
    return tuple(dict.fromkeys(paths))


@dataclass(frozen=True)
class Applied:
    """The outcome of applying a change set."""

    revision: int
    tree_hash: str
    changed_paths: tuple[str, ...]
    #: True when the workspace already had the post-image and nothing was written.
    replayed: bool
    #: True when the changes were found applied but unrecorded (a crash between write and record).
    recovered: bool = False

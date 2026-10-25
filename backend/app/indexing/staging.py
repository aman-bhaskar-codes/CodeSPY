"""Durably registered private source inputs; recovery requires exclusive startup."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from app.scanners.ownership import ScannerOwnership, cleanup_owned


class _NoContainer:
    async def inspect_owned(self, name: str) -> dict[str, str] | None:
        return None

    async def remove(self, identity: str) -> None:
        raise ValueError("source staging has no container")


class IndexInputStaging:
    def __init__(self, root: Path):
        if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
            raise ValueError("owned verified input directory required")
        self.root = root

    @asynccontextmanager
    async def open(self, image: str) -> AsyncIterator[Path]:
        owner = ScannerOwnership.create(self.root / ("index-stage-" + uuid4().hex), image)
        names: list[str] = []
        try:
            name = owner.reserve()
            names.append(name)
            root = owner.root / (name + ".input")
            root.mkdir(mode=0o700)
            yield root
        finally:
            await cleanup_owned(owner, _NoContainer(), names)
            self._remove_empty(owner)

    def _remove_empty(self, owner: ScannerOwnership) -> None:
        if sorted(p.name for p in owner.root.iterdir()) != ["ownership.json"]:
            raise ValueError("unexpected source staging files")
        owner.root.joinpath("ownership.json").unlink()
        owner.root.rmdir()

    async def recover_owned(self) -> None:
        """Run before accepting work; never sweep another live operation's input."""
        for root in sorted(self.root.glob("index-stage-*")):
            if (
                root.is_symlink()
                or not root.is_dir()
                or not re.fullmatch(r"index-stage-[0-9a-f]{32}", root.name)
            ):
                raise ValueError("foreign source staging directory")
            owner = ScannerOwnership.restore_recorded(root)
            await cleanup_owned(
                owner,
                _NoContainer(),
                [
                    path.name.removesuffix(".json")
                    for path in sorted(root.glob("codespy-scan-*.json"))
                ],
            )
            self._remove_empty(owner)

"""Exact-owner container records survive crashes and uncertain Docker outcomes."""

import asyncio
import json
import os
import re
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.scanners.process import NativeProcessError


class OwnershipController(Protocol):
    async def inspect_owned(self, name: str) -> dict[str, str] | None: ...
    async def remove(self, identity: str) -> None: ...


def _read(path: Path) -> dict[str, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
            raise ValueError("invalid scanner ownership record")
        raw = stream.read(1025)
    value = json.loads(raw)
    if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
        raise ValueError("invalid scanner ownership record")
    return value


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, value: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    _sync(path.parent)


@dataclass(frozen=True)
class ScannerOwnership:
    root: Path
    image: str
    marker: str

    @classmethod
    def create(cls, root: Path, image: str) -> "ScannerOwnership":
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("pinned local image ID required")
        root = root.absolute()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        marker = uuid4().hex
        _write(root / "ownership.json", {"image": image, "marker": marker})
        return cls(root, image, marker)

    @classmethod
    def restore(cls, root: Path, image: str) -> "ScannerOwnership":
        root = root.absolute()
        if root.is_symlink():
            raise ValueError("invalid scanner ownership workspace")
        value = _read(root / "ownership.json")
        if value.get("image") != image or not re.fullmatch(
            r"[0-9a-f]{32}", value.get("marker", "")
        ):
            raise ValueError("foreign scanner ownership workspace")
        return cls(root, image, value["marker"])

    @classmethod
    def restore_recorded(cls, root: Path) -> "ScannerOwnership":
        """Restore an exclusively owned workspace whose pinned image is recorded locally."""
        root = root.absolute()
        if root.is_symlink():
            raise ValueError("invalid scanner ownership workspace")
        value = _read(root / "ownership.json")
        image = value.get("image", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("invalid scanner ownership image")
        return cls.restore(root, image)

    def reserve(self) -> str:
        name = "codespy-scan-" + uuid4().hex
        _write(
            self.root / (name + ".json"), {"name": name, "image": self.image, "marker": self.marker}
        )
        return name

    async def remove(self, name: str, controller: OwnershipController) -> None:
        if not re.fullmatch(r"codespy-scan-[0-9a-f]{32}", name):
            raise ValueError("invalid scanner container identity")
        path = self.root / (name + ".json")
        try:
            record = _read(path)
        except FileNotFoundError:
            return
        if record != {"name": name, "image": self.image, "marker": self.marker}:
            raise ValueError("foreign scanner ownership record")
        actual = await controller.inspect_owned(name)
        if actual is not None:
            if (
                actual.get("image") != self.image
                or actual.get("marker") != self.marker
                or not re.fullmatch(r"[0-9a-f]{64}", actual.get("id", ""))
            ):
                raise ValueError("foreign scanner container")
            await controller.remove(actual["id"])
        staged = self.root / (name + ".input")
        if staged.is_symlink():
            raise ValueError("foreign scanner staging directory")
        if staged.exists():
            shutil.rmtree(staged)
        path.unlink(missing_ok=True)
        _sync(self.root)

    async def cleanup_pending(self, controller: OwnershipController) -> None:
        for path in sorted(self.root.glob("codespy-scan-*.json")):
            await self.remove(path.name.removesuffix(".json"), controller)


async def cleanup_owned(
    owner: ScannerOwnership, controller: OwnershipController, names: list[str]
) -> None:
    """Bound cleanup without discarding cancellation or uncertain ownership records."""
    if not names:
        return

    async def remove() -> None:
        async with asyncio.timeout(5):
            pending = False
            for name in reversed(names):
                try:
                    await owner.remove(name, controller)
                except Exception:
                    pending = True
            if pending:
                raise NativeProcessError("scanner_cleanup_pending")

    task = asyncio.create_task(remove())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Durable records retain cleanup work; cancellation stays cancellation.
        with suppress(Exception):
            await task
        raise
    except Exception:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError() from None
        raise NativeProcessError("scanner_cleanup_pending") from None

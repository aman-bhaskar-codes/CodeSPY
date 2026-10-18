"""Immutable objects in a private, descriptor-relative local store.

Only generated keys belong here; caller-provided filesystem paths never do.
"""

import asyncio
import contextlib
import hashlib
import os
import stat
from collections.abc import AsyncIterable, AsyncIterator, Callable
from pathlib import Path
from typing import TypeVar

from app.contracts.ingestion import ObjectDescriptor
from app.domain.errors import DuplicateConflict

T = TypeVar("T")


class LocalObjectStore:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("object root must be a real directory")
        os.chmod(self.root, 0o700)

    def _parent(self, key: str, *, create: bool = False) -> tuple[int, str]:
        parts = key.split("/")
        if (
            not key
            or len(key.encode()) > 1024
            or any(p in ("", ".", "..") or "\\" in p or "\x00" in p for p in parts)
        ):
            raise ValueError("invalid object key")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    async def _io(self, operation: Callable[[], T]) -> T:
        # Finish each owned filesystem operation before cancellation can close its fd.
        task = asyncio.create_task(asyncio.to_thread(operation))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def put_stage(
        self, key: str, chunks: AsyncIterable[bytes], max_bytes: int
    ) -> ObjectDescriptor:
        if not key.startswith("stage/") or max_bytes < 1:
            raise ValueError("invalid staging request")
        parent, name = self._parent(key, create=True)
        fd = -1
        created = False
        complete = False
        try:
            fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            created = True
            digest = hashlib.sha256()
            size = 0
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("object exceeds byte limit")
                digest.update(chunk)
                for offset in range(0, len(chunk), 65536):
                    piece = chunk[offset : offset + 65536]

                    def write_piece(value: bytes = piece) -> None:
                        view = memoryview(value)
                        while view:
                            written = os.write(fd, view)
                            if written == 0:
                                raise OSError("short object write")
                            view = view[written:]

                    await self._io(write_piece)
            await self._io(lambda: os.fsync(fd))
            complete = True
            return ObjectDescriptor(key=key, sha256=digest.hexdigest(), size_bytes=size)
        finally:
            if fd >= 0:
                os.close(fd)
            if created and not complete:
                os.unlink(name, dir_fd=parent)
            os.close(parent)

    def _verify(self, descriptor: ObjectDescriptor, *, collect: bool) -> bytes:
        parent, name = self._parent(descriptor.key)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size != descriptor.size_bytes:
                    raise DuplicateConflict("object descriptor mismatch")
                digest = hashlib.sha256()
                output = bytearray()
                size = 0
                while chunk := os.read(fd, 65536):
                    size += len(chunk)
                    if size > descriptor.size_bytes:
                        raise DuplicateConflict("object descriptor mismatch")
                    digest.update(chunk)
                    if collect:
                        output.extend(chunk)
                if size != descriptor.size_bytes or digest.hexdigest() != descriptor.sha256:
                    raise DuplicateConflict("object descriptor mismatch")
                return bytes(output)
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    async def open_verified(self, descriptor: ObjectDescriptor) -> AsyncIterator[bytes]:
        """Yield only after bounded independent verification of the complete object."""
        value = await self.read_verified(descriptor)
        for offset in range(0, len(value), 65536):
            yield value[offset : offset + 65536]

    async def read_verified(self, descriptor: ObjectDescriptor) -> bytes:
        if descriptor.version_id is not None:
            raise ValueError("local objects have no provider version ID")
        if not 0 <= descriptor.size_bytes <= 104857600:
            raise ValueError("object exceeds read limit")
        return await self._io(lambda: self._verify(descriptor, collect=True))

    async def put_if_absent(self, key: str, stage: ObjectDescriptor) -> ObjectDescriptor:
        if key.startswith("stage/"):
            raise ValueError("canonical key required")

        def publish() -> ObjectDescriptor:
            self._verify(stage, collect=False)
            src, src_name = self._parent(stage.key)
            dst, dst_name = self._parent(key, create=True)
            descriptor = stage.model_copy(update={"key": key})
            try:
                with contextlib.suppress(FileExistsError):
                    os.link(
                        src_name, dst_name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False
                    )
                self._verify(descriptor, collect=False)
                return descriptor
            finally:
                os.close(src)
                os.close(dst)

        return await self._io(publish)

    async def delete_exact(self, descriptor: ObjectDescriptor) -> None:
        if descriptor.version_id is not None:
            raise ValueError("local objects have no provider version ID")

        def delete() -> None:
            try:
                self._verify(descriptor, collect=False)
                parent, name = self._parent(descriptor.key)
                try:
                    os.unlink(name, dir_fd=parent)
                finally:
                    os.close(parent)
            except FileNotFoundError:
                return

        await self._io(delete)

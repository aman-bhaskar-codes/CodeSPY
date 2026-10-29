"""Owned isolated source-reader lifecycle for an explicitly configured local store."""

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from app.adapters.storage.local import LocalObjectStore
from app.ports.object_store import ObjectStore
from app.ports.source_reader import SourceReaderPort
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.source_read.adapter import DockerSourceReader


@asynccontextmanager
async def source_resources() -> AsyncIterator[tuple[ObjectStore | None, SourceReaderPort | None]]:
    root = os.environ.get("CODESPY_SOURCE_ROOT")
    image = os.environ.get("CODESPY_MCP_SOURCE_IMAGE")
    executable = os.environ.get("CODESPY_MCP_DOCKER_EXECUTABLE")
    socket = os.environ.get("CODESPY_MCP_DOCKER_SOCKET")
    if any((image, executable, socket)) and not all((root, image, executable, socket)):
        raise ValueError("complete isolated source-reader configuration required")
    if not root:
        yield None, None
        return
    path = Path(root)
    if not path.is_absolute() or not await asyncio.to_thread(
        lambda: path.is_dir() and not path.is_symlink()
    ):
        raise ValueError("existing absolute retained-object directory required")
    store = LocalObjectStore(path)
    if not image:
        yield store, None
        return
    assert executable is not None and socket is not None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("qualified local image ID required")
    if not Path(executable).is_absolute() or not Path(socket).is_absolute():
        raise ValueError("absolute Docker runtime paths required")
    workspace = Path(tempfile.mkdtemp(prefix="codespy-mcp-source-"))
    owner = ScannerOwnership.create(workspace / "owner", image)
    cleanup_complete = False
    controller = None
    try:
        controller = DockerController(Path(executable), Path(socket), workspace / "docker")
        reader = DockerSourceReader(owner, controller, store)
        try:
            yield store, reader
        finally:
            # Complete exact-owned cleanup before removing the recovery ledger.
            cleanup = asyncio.create_task(reader.recover_owned())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                cleanup_complete = True
                raise
            cleanup_complete = True
    finally:
        # A failed cleanup retains its exact-owner ledger for explicit recovery.
        if cleanup_complete or controller is None:
            shutil.rmtree(workspace)

"""Opt-in real Linux container qualification; bytes and storage are synthetic."""

import hashlib
import io
import os
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from app.contracts.ingestion import ObjectDescriptor
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderUnavailable
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.source_read.adapter import DockerSourceReader


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["plain", "github", "wrong_hash", "missing", "overflow"])
async def test_real_owned_reader(tmp_path: Path, scenario: str) -> None:
    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if not image:
        pytest.skip("owned pinned source-reader image not configured")
    content = b'def greet():\n    return "hello"\n'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as source:
        source.writestr("repo-sha/main.py" if scenario == "github" else "main.py", content)
    raw = archive.getvalue()

    class BytesStore:
        async def open_verified(self, descriptor):
            yield raw

    class BoundedController(DockerController):
        async def start(self, name, maximum):
            return await super().start(name, 1 if scenario == "overflow" else maximum)

    controller = BoundedController(
        Path("/usr/local/bin/docker"), Path.home() / ".docker/run/docker.sock", tmp_path / "config"
    )
    owner = ScannerOwnership.create(tmp_path / "owner", image)
    reader = DockerSourceReader(owner, controller, BytesStore())
    request = AdmittedSourceReadV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        file_id=uuid4(),
        archive=ObjectDescriptor(
            key="source/fixture", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        ),
        path="missing.py" if scenario == "missing" else "main.py",
        sha256="0" * 64 if scenario == "wrong_hash" else hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        strip_root=scenario == "github",
    )
    try:
        if scenario in {"wrong_hash", "missing", "overflow"}:
            with pytest.raises(SourceReaderUnavailable):
                await reader.read(request)
        else:
            assert await reader.read(request) == content
    finally:
        await reader.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_real_owner_restore_removes_container_after_lost_process(tmp_path: Path) -> None:
    from app.scanners.docker import SandboxLaunch

    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if not image:
        pytest.skip("owned pinned source-reader image not configured")
    controller = DockerController(
        Path("/usr/local/bin/docker"), Path.home() / ".docker/run/docker.sock", tmp_path / "config"
    )
    owner = ScannerOwnership.create(tmp_path / "owner", image)
    name = owner.reserve()
    root = owner.root / (name + ".input")
    root.mkdir()
    for filename in ("archive.zip", "manifest.json", "request.json"):
        (root / filename).write_bytes(b"{}")
    try:
        await controller.create(SandboxLaunch(name, owner.marker, image, root))
        await controller.verify_controls(name)
        restored = ScannerOwnership.restore(owner.root, image)
        await restored.cleanup_pending(controller)
        assert await controller.inspect_owned(name) is None
        assert not root.exists()
        assert not list(owner.root.glob("codespy-scan-*"))
    finally:
        await owner.cleanup_pending(controller)


@pytest.mark.asyncio
async def test_native_read_cancellation_after_create_keeps_cancellation_and_cleans(
    tmp_path: Path,
) -> None:
    import asyncio

    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if not image:
        pytest.skip("owned pinned source-reader image not configured")
    entered = asyncio.Event()

    class PausedController(DockerController):
        async def start(self, name, maximum):
            entered.set()
            await asyncio.Event().wait()
            return await super().start(name, maximum)

    content = b"print(1)"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("main.py", content)
    raw = archive.getvalue()

    class BytesStore:
        async def open_verified(self, descriptor):
            yield raw

    owner = ScannerOwnership.create(tmp_path / "owner", image)
    controller = PausedController(
        Path("/usr/local/bin/docker"), Path.home() / ".docker/run/docker.sock", tmp_path / "config"
    )
    reader = DockerSourceReader(owner, controller, BytesStore())
    request = AdmittedSourceReadV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        file_id=uuid4(),
        archive=ObjectDescriptor(
            key="source/fixture", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        ),
        path="main.py",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    task = asyncio.create_task(reader.read(request))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not list(owner.root.glob("codespy-scan-*"))
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reader.recover_owned()

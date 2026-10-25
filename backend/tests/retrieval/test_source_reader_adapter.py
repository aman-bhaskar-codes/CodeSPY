"""Adapter contract uses a controlled container controller, not a native qualification."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from app.contracts.ingestion import ObjectDescriptor
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderUnavailable
from app.source_read.adapter import DockerSourceReader


@pytest.mark.asyncio
async def test_unconfigured_reader_fails_closed() -> None:
    from app.source_read.adapter import UnavailableSourceReader

    request = AdmittedSourceReadV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        file_id=uuid4(),
        archive=ObjectDescriptor(key="source/fixture", sha256="0" * 64, size_bytes=0),
        path="main.py",
        sha256=hashlib.sha256(b"").hexdigest(),
        size_bytes=0,
    )
    with pytest.raises(SourceReaderUnavailable):
        await UnavailableSourceReader().read(request)


def test_no_unpinned_image_is_accepted(tmp_path: Path) -> None:
    from app.scanners.ownership import ScannerOwnership

    with pytest.raises(ValueError, match="pinned"):
        ScannerOwnership.create(tmp_path / "owner", "latest")
    assert DockerSourceReader is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["ok", "wrong_bytes", "diagnostic", "crash", "cancel", "timeout"]
)
async def test_adapter_verifies_results_and_always_cleans_owned_container(
    tmp_path: Path, outcome: str
) -> None:
    import asyncio

    from app.scanners.docker import SandboxLaunch
    from app.scanners.ownership import ScannerOwnership
    from app.scanners.process import NativeResult

    content = b"print(1)"
    raw = b"fixture archive bytes"
    owner = ScannerOwnership.create(tmp_path / "owner", "sha256:" + "a" * 64)

    class Store:
        async def open_verified(self, descriptor):
            yield raw

    class Controller:
        launch = None
        removed = False

        async def image_entrypoint(self, image):
            return ("python", "-m", "app.source_read.entrypoint")

        async def create(self, launch: SandboxLaunch):
            self.launch = launch

        async def inspect_owned(self, name):
            return {"image": owner.image, "marker": owner.marker, "id": "b" * 64}

        async def verify_controls(self, name):
            pass

        async def start(self, name, maximum):
            if outcome == "cancel":
                raise asyncio.CancelledError()
            if outcome == "timeout":
                raise TimeoutError()
            if outcome == "crash":
                raise OSError("canary private diagnostic")
            return NativeResult(
                0,
                b"wrong" if outcome == "wrong_bytes" else content,
                1 if outcome == "diagnostic" else 0,
            )

        async def remove(self, identity):
            self.removed = True

    controller = Controller()
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
    reader = DockerSourceReader(owner, controller, Store())
    if outcome == "ok":
        assert await reader.read(request) == content
    else:
        expected = asyncio.CancelledError if outcome == "cancel" else SourceReaderUnavailable
        with pytest.raises(expected):
            await reader.read(request)
    assert controller.removed
    assert not list(owner.root.glob("codespy-scan-*"))

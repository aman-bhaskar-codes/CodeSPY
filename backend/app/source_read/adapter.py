"""Source reader reuses the owned offline container envelope and crash cleanup ledger."""

import asyncio
import hashlib

from app.domain.canonical_json import canonical_bytes
from app.ports.object_store import ObjectStore
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderUnavailable
from app.scanners.adapter import ScannerController
from app.scanners.docker import SandboxLaunch
from app.scanners.ownership import ScannerOwnership, cleanup_owned
from app.scanners.process import NativeProcessError

ENTRYPOINT = ("python", "-m", "app.source_read.entrypoint")


class UnavailableSourceReader:
    async def read(self, request: AdmittedSourceReadV1) -> bytes:
        raise SourceReaderUnavailable("source_reader_unavailable")


class DockerSourceReader:
    def __init__(self, owner: ScannerOwnership, controller: ScannerController, store: ObjectStore):
        self.owner, self.controller, self.store = owner, controller, store

    async def read(self, request: AdmittedSourceReadV1) -> bytes:
        names: list[str] = []
        try:
            async with asyncio.timeout(20):
                request = AdmittedSourceReadV1.model_validate(request.model_dump())
                if request.archive.size_bytes > 104_857_600:
                    raise SourceReaderUnavailable("source_read_rejected")
                if await self.controller.image_entrypoint(self.owner.image) != ENTRYPOINT:
                    raise SourceReaderUnavailable("source_reader_unavailable")
                name = self.owner.reserve()
                names.append(name)
                root = self.owner.root / (name + ".input")
                root.mkdir(mode=0o700)
                size = 0
                digest = hashlib.sha256()
                with (root / "archive.zip").open("xb") as target:
                    async for block in self.store.open_verified(request.archive):
                        size += len(block)
                        if size > request.archive.size_bytes or size > 104_857_600:
                            raise SourceReaderUnavailable("source_read_rejected")
                        digest.update(block)
                        target.write(block)
                if (
                    size != request.archive.size_bytes
                    or digest.hexdigest() != request.archive.sha256
                ):
                    raise SourceReaderUnavailable("source_read_rejected")
                # The shared envelope mounts three fixed input files; this reader needs no manifest.
                (root / "manifest.json").write_bytes(b"{}")
                (root / "request.json").write_bytes(canonical_bytes(request.model_dump()))
                await self.controller.create(
                    SandboxLaunch(
                        name,
                        self.owner.marker,
                        self.owner.image,
                        root,
                    )
                )
                actual = await self.controller.inspect_owned(name)
                if (
                    actual is None
                    or actual.get("image") != self.owner.image
                    or actual.get("marker") != self.owner.marker
                ):
                    raise SourceReaderUnavailable("source_reader_unavailable")
                await self.controller.verify_controls(name)
                result = await self.controller.start(name, 2_097_152)
                if (
                    result.exit_code
                    or result.diagnostic_bytes
                    or len(result.output) != request.size_bytes
                    or hashlib.sha256(result.output).hexdigest() != request.sha256
                ):
                    raise SourceReaderUnavailable("source_read_rejected")
                return result.output
        except (TimeoutError, NativeProcessError, ValueError, OSError):
            raise SourceReaderUnavailable("source_reader_unavailable") from None
        finally:
            try:
                await cleanup_owned(self.owner, self.controller, names)
            except NativeProcessError:
                raise SourceReaderUnavailable("source_cleanup_pending") from None

    async def recover_owned(self) -> None:
        """Call during exclusive startup before accepting reads on a restored owner."""
        await self.owner.cleanup_pending(self.controller)

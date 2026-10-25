"""Owned offline index preparation with bounded input and durable cleanup."""

import asyncio
import hashlib
import os
import stat
from pathlib import Path

from app.contracts.indexing import IndexBuildConfigV1, IndexPublicationDraftV1, IndexSpecV1
from app.domain.canonical_json import canonical_bytes
from app.indexing.receipt import PreparedIndexReceipt, _issue
from app.ports.index_builder import (
    IndexBuilderUnavailable,
    IndexCapabilitiesV1,
    VerifiedIndexInputV1,
)
from app.scanners.adapter import ScannerController
from app.scanners.docker import SandboxLaunch
from app.scanners.ownership import ScannerOwnership, cleanup_owned
from app.scanners.process import NativeProcessError

ENTRYPOINT = ("python", "-m", "app.indexing.entrypoint")


class UnavailableIndexBuilder:
    async def validate(self, config: IndexBuildConfigV1) -> None:
        raise IndexBuilderUnavailable("index_builder_unavailable")

    async def capabilities(self) -> IndexCapabilitiesV1:
        raise IndexBuilderUnavailable("index_builder_unavailable")

    async def build(
        self, spec: IndexSpecV1, inputs: tuple[VerifiedIndexInputV1, ...], *, timeout_seconds: float
    ) -> PreparedIndexReceipt:
        raise IndexBuilderUnavailable("index_builder_unavailable")

    async def recover_owned(self) -> None:
        return None


class DockerIndexBuilder:
    def __init__(
        self,
        owner: ScannerOwnership,
        controller: ScannerController,
        verified_root: Path,
        *,
        runtime_source_sha256: str,
    ):
        IndexCapabilitiesV1(runtime_source_sha256=runtime_source_sha256, max_request_bytes=98_368)
        if (
            not verified_root.is_absolute()
            or verified_root.is_symlink()
            or not verified_root.is_dir()
        ):
            raise ValueError("owned verified input directory required")
        self.owner, self.controller = owner, controller
        self.verified_root = verified_root.resolve()
        self.runtime_source_sha256 = runtime_source_sha256

    async def validate(self, config: IndexBuildConfigV1) -> None:
        config = IndexBuildConfigV1.model_validate(config.model_dump())
        if (
            config.runtime_image != self.owner.image
            or config.runtime_source_sha256 != self.runtime_source_sha256
        ):
            raise IndexBuilderUnavailable("index_builder_unavailable")
        await self.capabilities()

    async def capabilities(self) -> IndexCapabilitiesV1:
        try:
            async with asyncio.timeout(30):
                result = IndexCapabilitiesV1.model_validate_json(await self._exchange(None, ()))
                if result.runtime_source_sha256 != self.runtime_source_sha256:
                    raise ValueError("runtime differs")
                return result
        except (ValueError, OSError, TimeoutError, NativeProcessError):
            raise IndexBuilderUnavailable("index_builder_unavailable") from None

    async def build(
        self, spec: IndexSpecV1, inputs: tuple[VerifiedIndexInputV1, ...], *, timeout_seconds: float
    ) -> PreparedIndexReceipt:
        try:
            if not 0 < timeout_seconds <= 180:
                raise ValueError("invalid index deadline")
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            async with asyncio.timeout(timeout_seconds):
                spec = IndexSpecV1.model_validate(spec.model_dump())
                if (
                    spec.config.runtime_image != self.owner.image
                    or spec.config.runtime_source_sha256 != self.runtime_source_sha256
                ):
                    raise ValueError("runtime differs")
                if len(inputs) != len(spec.files) or {i.file.file_id for i in inputs} != {
                    f.file_id for f in spec.files
                }:
                    raise ValueError("input selection differs")
                await self.capabilities()
                raw = await self._exchange(spec, inputs)
                draft = IndexPublicationDraftV1.model_validate_json(raw)
                draft.validate_for(spec)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError()
                receipt = _issue(spec, draft)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError()
                return receipt
        except TimeoutError:
            raise IndexBuilderUnavailable(
                "index_builder_unavailable", failure_code="index_timeout"
            ) from None
        except ValueError:
            raise IndexBuilderUnavailable(
                "index_builder_unavailable", failure_code="integrity_failure"
            ) from None
        except OSError:
            raise IndexBuilderUnavailable(
                "index_builder_unavailable", failure_code="index_process_failed"
            ) from None
        except NativeProcessError as error:
            code = "index_cleanup_pending" if "cleanup" in str(error) else "index_process_failed"
            raise IndexBuilderUnavailable("index_builder_unavailable", failure_code=code) from None

    def _copy_inputs(
        self, root: Path, spec: IndexSpecV1, inputs: tuple[VerifiedIndexInputV1, ...]
    ) -> None:
        supplied = {i.file.file_id: i for i in inputs}
        rows: list[dict[str, object]] = []
        offset = 0
        with (root / "archive.zip").open("xb") as output:
            for file in spec.files:
                item = supplied[file.file_id]
                path = item.owned_path
                relative = path.relative_to(self.verified_root)
                if (
                    ".." in relative.parts
                    or item.file != file
                    or any(
                        (self.verified_root / Path(*relative.parts[:i])).is_symlink()
                        for i in range(1, len(relative.parts) + 1)
                    )
                ):
                    raise ValueError("unverified input")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size != file.size_bytes:
                        raise ValueError("unverified input")
                    raw = stream.read(file.size_bytes + 1)
                if len(raw) != file.size_bytes or hashlib.sha256(raw).hexdigest() != file.sha256:
                    raise ValueError("unverified input")
                rows.append(
                    {"file_id": str(file.file_id), "offset": offset, "size_bytes": len(raw)}
                )
                offset += len(raw)
                if offset > 16_777_216:
                    raise ValueError("input limit")
                output.write(raw)
        (root / "manifest.json").write_bytes(canonical_bytes({"files": rows}))

    async def _exchange(
        self, spec: IndexSpecV1 | None, inputs: tuple[VerifiedIndexInputV1, ...]
    ) -> bytes:
        names: list[str] = []
        try:
            if await self.controller.image_entrypoint(self.owner.image) != ENTRYPOINT:
                raise ValueError("runtime differs")
            name = self.owner.reserve()
            names.append(name)
            root = self.owner.root / (name + ".input")
            root.mkdir(mode=0o700)
            if spec is None:
                request: dict[str, object] = {"operation": "capabilities"}
                (root / "archive.zip").write_bytes(b"")
                (root / "manifest.json").write_bytes(b'{"files":[]}')
            else:
                request = {"operation": "build", "spec": spec.model_dump()}
                self._copy_inputs(root, spec, inputs)
            (root / "request.json").write_bytes(canonical_bytes(request))
            await self.controller.create(
                SandboxLaunch(name, self.owner.marker, self.owner.image, root)
            )
            actual = await self.controller.inspect_owned(name)
            if (
                actual is None
                or actual.get("image") != self.owner.image
                or actual.get("marker") != self.owner.marker
            ):
                raise ValueError("ownership differs")
            await self.controller.verify_controls(name)
            maximum = 65_536 if spec is None else 25_165_824
            result = await self.controller.start(name, maximum)
            if result.exit_code or result.diagnostic_bytes or len(result.output) > maximum:
                raise ValueError("index output invalid")
            return result.output
        finally:
            await cleanup_owned(self.owner, self.controller, names)

    async def recover_owned(self) -> None:
        await self.owner.cleanup_pending(self.controller)

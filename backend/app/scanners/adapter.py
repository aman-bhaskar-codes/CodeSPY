"""Injectable source-to-scanner orchestration with durable owned cleanup."""

import asyncio
import os
from pathlib import Path
from typing import Protocol

from app.contracts.scanners import ScannerArchiveV1, ScannerRequestV1, ScannerResultV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.ports.scanner import ScannerStager
from app.scanners.attestation import ScannerAttestationV1
from app.scanners.docker import SandboxLaunch
from app.scanners.ownership import OwnershipController, ScannerOwnership, cleanup_owned
from app.scanners.process import NativeProcessError, NativeResult
from app.scanners.receipts import BrokerReceipt, _issue
from app.scanners.runtime import RuntimeManifest


class ScannerController(OwnershipController, Protocol):
    async def image_entrypoint(self, image: str) -> tuple[str, ...]: ...
    async def create(self, launch: SandboxLaunch) -> None: ...
    async def verify_controls(self, name: str) -> None: ...
    async def start(self, name: str, maximum: int) -> NativeResult: ...


class DockerScannerAdapter:
    def __init__(
        self,
        owner: ScannerOwnership,
        controller: ScannerController,
        stager: ScannerStager,
        manifest: RuntimeManifest,
        *,
        expected_entrypoint: tuple[str, ...],
    ):
        if not expected_entrypoint:
            raise ValueError("configured scanner entrypoint required")
        self.owner, self.controller, self.stager = owner, controller, stager
        self.manifest, self.expected_entrypoint = manifest, expected_entrypoint

    async def scan(self, request: ScannerRequestV1) -> ScannerResultV1:
        return (await self.scan_attested(request)).result

    async def scan_attested(
        self, request: ScannerRequestV1
    ) -> BrokerReceipt[ScannerRequestV1, ScannerResultV1]:
        names: list[str] = []
        expires = asyncio.get_running_loop().time() + request.profile.deadline_seconds
        try:
            async with asyncio.timeout(request.profile.deadline_seconds):
                runtime = self.manifest.select(request.profile)
                if (
                    await self.controller.image_entrypoint(self.owner.image)
                    != self.expected_entrypoint
                ):
                    raise NativeProcessError("scanner_runtime_unavailable")
                name = self.owner.reserve()
                names.append(name)
                root = self.owner.root / (name + ".input")
                root.mkdir(mode=0o700)
                source = await self.stager.stage(request, root)
                if source.request != request:
                    raise NativeProcessError("scanner_source_invalid")
                # Revalidate internal contracts; model_copy is not a validation boundary.
                source = ScannerArchiveV1.model_validate(source.model_dump())
                with (root / "request.json").open("xb") as stream:
                    stream.write(canonical_bytes(source.model_dump()))
                await self.controller.create(
                    SandboxLaunch(name, self.owner.marker, self.owner.image, root)
                )
                actual = await self.controller.inspect_owned(name)
                if (
                    actual is None
                    or actual.get("image") != self.owner.image
                    or actual.get("marker") != self.owner.marker
                ):
                    raise NativeProcessError("scanner_ownership_invalid")
                await self.controller.verify_controls(name)
                native = await self.controller.start(name, request.profile.output_bytes)
                if native.diagnostic_bytes:
                    raise NativeProcessError("scanner_output_invalid")
                result = ScannerResultV1.model_validate_json(native.output)
                result.validate_for(request)
                await self._attest(source, result, root, names)
                if asyncio.get_running_loop().time() >= expires:
                    raise TimeoutError()
                return _issue(request, result, self.owner.image, runtime.version)
        except TimeoutError:
            raise NativeProcessError("scanner_timeout") from None
        except (ValueError, OSError):
            raise NativeProcessError("scanner_output_invalid") from None
        finally:
            await cleanup_owned(self.owner, self.controller, names)

    async def _attest(
        self,
        source: ScannerArchiveV1,
        result: ScannerResultV1,
        original: Path,
        names: list[str],
    ) -> None:
        # A fresh verifier reads admitted bytes in isolation and invokes no native tools.
        name = self.owner.reserve()
        names.append(name)
        root = self.owner.root / (name + ".input")
        root.mkdir(mode=0o700)
        for filename in ("archive.zip", "manifest.json"):
            os.link(original / filename, root / filename)
        value = ScannerAttestationV1(source=source, result=result)
        with (root / "request.json").open("xb") as stream:
            stream.write(canonical_bytes(value.model_dump()))
        await self.controller.create(
            SandboxLaunch(name, self.owner.marker, self.owner.image, root, mode="attest")
        )
        actual = await self.controller.inspect_owned(name)
        if (
            actual is None
            or actual.get("image") != self.owner.image
            or actual.get("marker") != self.owner.marker
        ):
            raise NativeProcessError("scanner_ownership_invalid")
        await self.controller.verify_controls(name)
        verified = await self.controller.start(name, 65)
        if verified.diagnostic_bytes or verified.output != semantic_digest(
            result.model_dump()
        ).encode("ascii"):
            raise NativeProcessError("scanner_output_invalid")

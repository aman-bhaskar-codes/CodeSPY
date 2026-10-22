"""Run offline dependency assessment using the existing owned sandbox controls."""

import asyncio

from app.contracts.dependencies import DependencyRequestV1, DependencyResultV1
from app.domain.canonical_json import canonical_bytes
from app.ports.dependencies import DependencyStager
from app.scanners.adapter import ScannerController
from app.scanners.advisories import AdvisoryBundle
from app.scanners.dependency_worker import DependencyInputV1
from app.scanners.docker import SandboxLaunch
from app.scanners.ownership import ScannerOwnership, cleanup_owned
from app.scanners.process import NativeProcessError
from app.scanners.receipts import BrokerReceipt, _issue


class DockerDependencyAdapter:
    def __init__(
        self,
        owner: ScannerOwnership,
        controller: ScannerController,
        stager: DependencyStager,
        bundle: AdvisoryBundle,
        *,
        expected_entrypoint: tuple[str, ...],
    ):
        if not expected_entrypoint:
            raise ValueError("configured dependency entrypoint required")
        self.owner, self.controller, self.stager, self.bundle = owner, controller, stager, bundle
        self.expected_entrypoint = expected_entrypoint

    async def assess(self, request: DependencyRequestV1) -> DependencyResultV1:
        return (await self.assess_attested(request)).result

    async def assess_attested(
        self, request: DependencyRequestV1
    ) -> BrokerReceipt[DependencyRequestV1, DependencyResultV1]:
        names: list[str] = []
        expires = asyncio.get_running_loop().time() + 180
        try:
            async with asyncio.timeout(180):
                if request.runtime_digest != self.owner.image.removeprefix("sha256:"):
                    raise NativeProcessError("scanner_runtime_unavailable")
                if request.bundle_digest != self.bundle.digest():
                    raise NativeProcessError("scanner_source_invalid")
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
                value = DependencyInputV1(source=source, bundle=self.bundle)
                value = DependencyInputV1.model_validate(value.model_dump())
                with (root / "request.json").open("xb") as stream:
                    stream.write(canonical_bytes(value.model_dump()))
                await self.controller.create(
                    SandboxLaunch(
                        name, self.owner.marker, self.owner.image, root, mode="dependencies"
                    )
                )
                actual = await self.controller.inspect_owned(name)
                if (
                    actual is None
                    or actual.get("image") != self.owner.image
                    or actual.get("marker") != self.owner.marker
                ):
                    raise NativeProcessError("scanner_ownership_invalid")
                await self.controller.verify_controls(name)
                native = await self.controller.start(name, 2097152)
                if native.diagnostic_bytes:
                    raise NativeProcessError("scanner_output_invalid")
                result = DependencyResultV1.model_validate_json(native.output)
                result.validate_for(request)
                if asyncio.get_running_loop().time() >= expires:
                    raise TimeoutError()
                return _issue(request, result, self.owner.image, request.parser_profile)
        except TimeoutError:
            raise NativeProcessError("scanner_timeout") from None
        except (ValueError, OSError):
            raise NativeProcessError("scanner_output_invalid") from None
        finally:
            await cleanup_owned(self.owner, self.controller, names)

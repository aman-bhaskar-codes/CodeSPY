"""Explicit local scanner composition; no provider, credential or runtime discovery."""

import re
from collections.abc import Callable, Mapping
from typing import TypeVar

from app.contracts.dependencies import DependencyRequestV1, DependencyResultV1
from app.contracts.scan_jobs import ScanConfigV1
from app.contracts.scanners import ScannerRequestV1, ScannerResultV1
from app.domain.canonical_json import semantic_digest
from app.ports.object_store import ObjectStore
from app.ports.scan_execution import AttestedDependencyPort, AttestedScannerPort
from app.scanners.adapter import DockerScannerAdapter, ScannerController
from app.scanners.advisories import AdvisoryBundle
from app.scanners.dependency_adapter import DockerDependencyAdapter
from app.scanners.finding_catalog import FindingCatalogV1
from app.scanners.ownership import ScannerOwnership
from app.scanners.receipts import BrokerReceipt
from app.scanners.runtime import RuntimeManifest
from app.services.analysis_sources import LocatedSource
from app.services.dependency_sources import BoundDependencyStager
from app.services.scanner_sources import BoundScannerStager

Q = TypeVar("Q", ScannerRequestV1, DependencyRequestV1)
R = TypeVar("R", ScannerResultV1, DependencyResultV1)


class LocalScanCapabilities:
    def __init__(
        self,
        manifest: RuntimeManifest,
        image_digest: str,
        catalogs: Mapping[str, FindingCatalogV1],
        bundles: Mapping[str, AdvisoryBundle],
        object_store: ObjectStore,
        controller: ScannerController,
        ownership_factory: Callable[[], ScannerOwnership],
        expected_entrypoint: tuple[str, ...],
        *,
        recovery_owners: tuple[ScannerOwnership, ...] = (),
    ):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) or not expected_entrypoint:
            raise ValueError("pinned scanner runtime and entrypoint required")
        self.manifest = RuntimeManifest.model_validate(manifest.model_dump())
        self.image, self.store, self.controller = image_digest, object_store, controller
        self.ownership_factory, self.entrypoint = ownership_factory, expected_entrypoint
        self._catalogs = {
            k: FindingCatalogV1.model_validate(v.model_dump()) for k, v in catalogs.items()
        }
        self._bundles = {
            k: AdvisoryBundle.model_validate(v.model_dump()) for k, v in bundles.items()
        }
        if any(k != v.semantic_hash() for k, v in self._catalogs.items()) or any(
            k != v.digest() for k, v in self._bundles.items()
        ):
            raise ValueError("registered scan metadata digest differs")
        if any(o.image != self.image for o in recovery_owners):
            raise ValueError("foreign scanner recovery owner")
        self._pending = {str(o.root): o for o in recovery_owners}
        self._active: set[str] = set()

    def catalog(self, digest: str) -> FindingCatalogV1:
        value = self._catalogs.get(digest)
        if value is None or value.semantic_hash() != digest:
            raise ValueError("unregistered finding catalog")
        return FindingCatalogV1.model_validate(value.model_dump())

    def bundle(self, digest: str) -> AdvisoryBundle:
        value = self._bundles.get(digest)
        if value is None or value.digest() != digest:
            raise ValueError("unregistered advisory bundle")
        return AdvisoryBundle.model_validate(value.model_dump())

    def validate(self, config: ScanConfigV1) -> None:
        config = ScanConfigV1.model_validate(config.model_dump())
        if config.runtime_digest != self.image.removeprefix("sha256:"):
            raise ValueError("unregistered scanner runtime")
        catalog = self.catalog(config.catalog_digest)
        for selection in config.native_selections:
            runtime = self.manifest.select(selection.profile)
            for rule in selection.profile.rule_ids:
                if not any(
                    p.rule_id == rule
                    and p.profile_digest == semantic_digest(selection.profile.model_dump())
                    and p.provider_version == runtime.version
                    for p in catalog.native
                ):
                    raise ValueError("unregistered scanner policy")
        if config.dependency_selection is not None:
            self.bundle(config.dependency_selection.bundle_digest)

    def _owner(self) -> ScannerOwnership:
        owner = self.ownership_factory()
        key = str(owner.root)
        if owner.image != self.image or key in self._active or key in self._pending:
            raise ValueError("scanner ownership must be distinct and image bound")
        self._active.add(key)
        return owner

    def _done(self, owner: ScannerOwnership) -> None:
        self._active.discard(str(owner.root))
        self._pending[str(owner.root)] = owner

    def native(self, request: ScannerRequestV1, located: LocatedSource) -> AttestedScannerPort:
        self.manifest.select(request.profile)
        owner = self._owner()
        adapter = DockerScannerAdapter(
            owner,
            self.controller,
            BoundScannerStager(self.store, located, request.scope, request.repository_id),
            self.manifest,
            expected_entrypoint=self.entrypoint,
        )
        registry = self

        class Bound:
            async def scan_attested(
                self, received: ScannerRequestV1
            ) -> BrokerReceipt[ScannerRequestV1, ScannerResultV1]:
                try:
                    if received != request:
                        raise ValueError("scanner request binding differs")
                    return await adapter.scan_attested(received)
                finally:
                    registry._done(owner)

        return Bound()

    def dependencies(
        self, request: DependencyRequestV1, located: LocatedSource
    ) -> AttestedDependencyPort:
        bundle = self.bundle(request.bundle_digest)
        owner = self._owner()
        adapter = DockerDependencyAdapter(
            owner,
            self.controller,
            BoundDependencyStager(self.store, located, request.scope, request.repository_id),
            bundle,
            expected_entrypoint=self.entrypoint,
        )
        registry = self

        class Bound:
            async def assess_attested(
                self, received: DependencyRequestV1
            ) -> BrokerReceipt[DependencyRequestV1, DependencyResultV1]:
                try:
                    if received != request:
                        raise ValueError("dependency request binding differs")
                    return await adapter.assess_attested(received)
                finally:
                    registry._done(owner)

        return Bound()

    async def recover_owned(self) -> int:
        recovered = 0
        for key, owner in tuple(self._pending.items()):
            if key not in self._active:
                await owner.cleanup_pending(self.controller)
                self._pending.pop(key, None)
                recovered += 1
        return recovered

"""Trusted execution capabilities are injected; wire DTOs cannot mint receipts."""

from typing import TYPE_CHECKING, Protocol

from app.contracts.dependencies import DependencyRequestV1, DependencyResultV1
from app.contracts.scan_jobs import ScanConfigV1
from app.contracts.scanners import ScannerRequestV1, ScannerResultV1
from app.scanners.advisories import AdvisoryBundle
from app.scanners.finding_catalog import FindingCatalogV1
from app.scanners.receipts import BrokerReceipt

if TYPE_CHECKING:
    from app.services.analysis_sources import LocatedSource


class AttestedScannerPort(Protocol):
    async def scan_attested(
        self, request: ScannerRequestV1
    ) -> BrokerReceipt[ScannerRequestV1, ScannerResultV1]: ...


class AttestedDependencyPort(Protocol):
    async def assess_attested(
        self, request: DependencyRequestV1
    ) -> BrokerReceipt[DependencyRequestV1, DependencyResultV1]: ...


class ScanCapabilities(Protocol):
    def validate(self, config: ScanConfigV1) -> None: ...
    def catalog(self, digest: str) -> FindingCatalogV1: ...
    def bundle(self, digest: str) -> AdvisoryBundle: ...
    def native(
        self, request: ScannerRequestV1, located: "LocatedSource"
    ) -> AttestedScannerPort: ...
    def dependencies(
        self, request: DependencyRequestV1, located: "LocatedSource"
    ) -> AttestedDependencyPort: ...
    async def recover_owned(self) -> int: ...

"""Bind authorized persisted source descriptors to one scoped scanner execution."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.contracts.scanners import ScannerArchiveV1, ScannerRequestV1
from app.contracts.structure import SourceDescriptorV1, SourcePolicyV1
from app.domain.errors import ScopeViolation
from app.domain.scope import RunRef
from app.ports.object_store import ObjectStore
from app.services.analysis_sources import LocatedSource, stage_admitted_artifact


@dataclass(frozen=True)
class BoundScannerStager:
    store: ObjectStore
    located: LocatedSource
    scope: RunRef
    repository_id: UUID

    async def stage(self, request: ScannerRequestV1, root: Path) -> ScannerArchiveV1:
        if request.scope != self.scope or request.repository_id != self.repository_id:
            raise ScopeViolation("scanner source scope differs")
        source = ScannerArchiveV1(
            request=request,
            archive=SourceDescriptorV1(
                path="archive.zip",
                sha256=self.located.archive.sha256,
                size_bytes=self.located.archive.size_bytes,
            ),
            manifest=SourceDescriptorV1(
                path="manifest.json",
                sha256=self.located.manifest.sha256,
                size_bytes=self.located.manifest.size_bytes,
            ),
            files=self.located.files,
            policy=SourcePolicyV1(
                policy_hash=self.located.policy.digest(),
                strip_single_root=self.located.policy.github_strip_root,
                admission=self.located.policy,
            ),
        )
        await stage_admitted_artifact(self.store, self.located, root)
        return source

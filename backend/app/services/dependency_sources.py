"""Bind admitted dependency metadata to an authorized repository and run."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.contracts.dependencies import DependencyArchiveV1, DependencyRequestV1
from app.contracts.structure import SourceDescriptorV1, SourcePolicyV1
from app.domain.errors import ScopeViolation
from app.domain.scope import RunRef
from app.ports.object_store import ObjectStore
from app.services.analysis_sources import LocatedSource, stage_admitted_artifact


@dataclass(frozen=True)
class BoundDependencyStager:
    store: ObjectStore
    located: LocatedSource
    scope: RunRef
    repository_id: UUID

    async def stage(self, request: DependencyRequestV1, root: Path) -> DependencyArchiveV1:
        if request.scope != self.scope or request.repository_id != self.repository_id:
            raise ScopeViolation("dependency source scope differs")
        source = DependencyArchiveV1(
            request=request,
            files=self.located.files,
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
            policy=SourcePolicyV1(
                policy_hash=self.located.policy.digest(),
                strip_single_root=self.located.policy.github_strip_root,
                admission=self.located.policy,
            ),
        )
        await stage_admitted_artifact(self.store, self.located, root)
        return source

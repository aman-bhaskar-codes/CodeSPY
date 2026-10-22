"""Create reusable scan configurations and durable scoped work atomically."""

from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.dependencies import DependencyArchiveV1
from app.contracts.scan_jobs import (
    EnqueueScanJobV4,
    ScanConfigV1,
    ScanJobRecordV4,
    ScanSpecRecordV1,
)
from app.contracts.scanners import ScannerArchiveV1
from app.contracts.structure import SourceDescriptorV1, SourcePolicyV1
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import DuplicateConflict
from app.domain.scope import RunRef, TenantContext
from app.models.scan_tables import scan_jobs, scan_specs
from app.ports.scan_execution import ScanCapabilities
from app.repositories.job_repository import scan_job_record
from app.repositories.scan_repository import ScanRepository, spec_record
from app.security.authorization import lock_scope
from app.services.analysis_sources import locate_source
from app.services.jobs import JobService


class ScanJobService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], capabilities: ScanCapabilities):
        self.factory, self.capabilities = factory, capabilities
        self.jobs = JobService(factory)

    async def create(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        config: ScanConfigV1,
        request_key: str,
    ) -> tuple[ScanSpecRecordV1, ScanJobRecordV4]:
        config = ScanConfigV1.model_validate(config.model_dump())
        if request_key.startswith(("job:", "child:")):
            raise ValueError("reserved request namespace")
        self.capabilities.validate(config)
        async with transaction(self.factory) as session:
            # API role may lock repository/capacity, not mutate or lock source snapshots.
            await lock_scope(session, ctx.org_id, ctx.principal_id, repository_id)
            existing = (
                (
                    await session.execute(
                        select(scan_jobs).where(
                            scan_jobs.c.org_id == ctx.org_id,
                            scan_jobs.c.request_key == request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["operation"] != "scan_snapshot"
                    or existing["principal_id"] != ctx.principal_id
                    or existing["repository_id"] != repository_id
                    or existing["snapshot_id"] != snapshot_id
                    or existing["config_hash"] != config.semantic_hash()
                    or existing["retry_of_job_id"] is not None
                ):
                    raise DuplicateConflict("request key has different input")
                stored = await ScanRepository(session).get_spec(
                    ctx, repository_id, existing["scan_spec_id"]
                )
                if stored is None:
                    raise DuplicateConflict("scan specification unavailable")
                return stored, scan_job_record(dict(existing))
            located = await locate_source(session, ctx.org_id, repository_id, snapshot_id)
            # Existing archive DTOs validate persisted eligibility/format/hash selection.
            scope = RunRef(org_id=ctx.org_id, snapshot_id=snapshot_id, run_id=uuid4())
            publication = config.publication_spec(scope, repository_id, await database_now(session))
            archive = SourceDescriptorV1(
                path="archive.zip",
                sha256=located.archive.sha256,
                size_bytes=located.archive.size_bytes,
            )
            manifest = SourceDescriptorV1(
                path="manifest.json",
                sha256=located.manifest.sha256,
                size_bytes=located.manifest.size_bytes,
            )
            policy = SourcePolicyV1(
                policy_hash=located.policy.digest(),
                admission=located.policy,
                strip_single_root=located.policy.github_strip_root,
            )
            for request in publication.native_requests:
                ScannerArchiveV1(
                    request=request,
                    archive=archive,
                    manifest=manifest,
                    files=located.files,
                    policy=policy,
                )
            if publication.dependency_request is not None:
                DependencyArchiveV1(
                    request=publication.dependency_request,
                    archive=archive,
                    manifest=manifest,
                    files=located.files,
                    policy=policy,
                )
            row = (
                (
                    await session.execute(
                        insert(scan_specs)
                        .values(
                            org_id=ctx.org_id,
                            id=uuid4(),
                            repository_id=repository_id,
                            snapshot_id=snapshot_id,
                            principal_id=ctx.principal_id,
                            config_hash=config.semantic_hash(),
                            config_json=config.model_dump(mode="json"),
                            canonical_text=canonical_bytes(config.model_dump()).decode(),
                        )
                        .returning(scan_specs)
                    )
                )
                .mappings()
                .one()
            )
            spec = spec_record(row)
            job = await self.jobs._enqueue(
                session,
                ctx,
                EnqueueScanJobV4(
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    scan_spec_id=spec.id,
                    config_hash=spec.config_hash,
                    request_key=request_key,
                ),
            )
            return spec, scan_job_record(job)

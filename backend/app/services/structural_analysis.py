"""Structural analysis service managing specs, jobs, and scoped report reads."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.jobs import EnqueueJobV3, JobRecordV3
from app.contracts.structure import (
    AnalysisConfigV1,
    AnalysisSpecV1,
    ArchitectureReportV1,
    stored_report,
)
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import TenantContext
from app.models.phase3_tables import structural_jobs, structural_reports
from app.models.tables import source_snapshots
from app.repositories.job_repository import job_record_v3
from app.repositories.structural_repository import StructuralRepository, published_reports
from app.security.authorization import lock_scope, require_repository
from app.services.jobs import JobService


async def replay_spec(
    repo: StructuralRepository,
    ctx: TenantContext,
    existing: Mapping[str, Any],
    repository_id: UUID,
    snapshot_id: UUID,
    config: AnalysisConfigV1,
) -> AnalysisSpecV1:
    if (
        existing["operation"] != "analyze_structure"
        or existing["repository_id"] != repository_id
        or existing["snapshot_id"] != snapshot_id
        or existing["config_hash"] != config.semantic_hash()
    ):
        raise DuplicateConflict("request key has different input")
    spec = await repo.get_spec(ctx, repository_id, existing["analysis_spec_id"])
    if spec is None:
        raise DuplicateConflict("analysis specification unavailable")
    return spec


class StructuralAnalysisService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory
        self.jobs = JobService(factory)

    async def create_analysis_spec(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        config: AnalysisConfigV1,
        request_key: str,
    ) -> tuple[AnalysisSpecV1, JobRecordV3]:
        config_hash = semantic_digest(config.model_dump(mode="json"))
        spec_id = uuid4()
        spec = AnalysisSpecV1(
            org_id=ctx.org_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            id=spec_id,
            principal_id=ctx.principal_id,
            config=config,
            config_hash=config_hash,
            toolchain_hash=config.toolchain_hash,
        )

        async with transaction(self.factory) as s:
            await lock_scope(s, ctx.org_id, ctx.principal_id, repository_id, snapshot_id)
            # lock_scope serializes request-key resolution under the org work lock.
            existing = (
                (
                    await s.execute(
                        select(structural_jobs).where(
                            structural_jobs.c.org_id == ctx.org_id,
                            structural_jobs.c.request_key == request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                stored = await replay_spec(
                    StructuralRepository(s), ctx, dict(existing), repository_id, snapshot_id, config
                )
                return stored, job_record_v3(dict(existing))
            # Verify snapshot is sealed
            sealed = await s.scalar(
                select(source_snapshots.c.id).where(
                    source_snapshots.c.org_id == ctx.org_id,
                    source_snapshots.c.repository_id == repository_id,
                    source_snapshots.c.id == snapshot_id,
                    source_snapshots.c.state == "sealed",
                )
            )
            if not sealed:
                raise ScopeViolation("snapshot unavailable or not sealed")

            repo = StructuralRepository(s)
            await repo.create_spec(ctx, spec)

            enqueue_req = EnqueueJobV3(
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                analysis_spec_id=spec_id,
                operation="analyze_structure",
                request_key=request_key,
                config_hash=config_hash,
            )
            job_row = await self.jobs._enqueue(s, ctx, enqueue_req)

        return spec, job_record_v3(job_row)

    async def get_spec(
        self, ctx: TenantContext, repository_id: UUID, spec_id: UUID
    ) -> AnalysisSpecV1 | None:
        async with transaction(self.factory) as s:
            repo = StructuralRepository(s)
            return await repo.get_spec(ctx, repository_id, spec_id)

    async def get_report_by_run(
        self, ctx: TenantContext, snapshot_id: UUID, run_id: UUID
    ) -> ArchitectureReportV1 | None:
        async with transaction(self.factory) as s:
            repo = StructuralRepository(s)
            return await repo.get_report_by_run(ctx, snapshot_id, run_id)

    async def get_report_by_snapshot(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID
    ) -> ArchitectureReportV1 | None:
        async with transaction(self.factory) as s:
            await require_repository(s, ctx.org_id, ctx.principal_id, repository_id)
            stmt = (
                published_reports()
                .where(
                    structural_reports.c.org_id == ctx.org_id,
                    structural_reports.c.repository_id == repository_id,
                    structural_reports.c.snapshot_id == snapshot_id,
                )
                .order_by(
                    structural_reports.c.created_at.desc(), structural_reports.c.run_id.desc()
                )
                .limit(1)
            )
            row = (await s.execute(stmt)).mappings().one_or_none()
            if not row:
                return None
            return stored_report(row["report_json"])

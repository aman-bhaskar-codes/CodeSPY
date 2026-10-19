"""Repository for immutable analysis_specs and structural_reports."""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.structure import AnalysisConfigV1, AnalysisSpecV1, ArchitectureReportV1
from app.domain.scope import TenantContext
from app.models.phase3_tables import analysis_specs, structural_reports
from app.models.tables import analysis_runs
from app.repositories.identity_repository import authorize
from app.security.authorization import require_repository


def published_reports() -> Select[Any]:
    return (
        select(structural_reports)
        .join(
            analysis_runs,
            (analysis_runs.c.org_id == structural_reports.c.org_id)
            & (analysis_runs.c.snapshot_id == structural_reports.c.snapshot_id)
            & (analysis_runs.c.id == structural_reports.c.run_id),
        )
        .where(analysis_runs.c.status.in_(["completed", "partial"]))
    )


class StructuralRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_spec(self, ctx: TenantContext, spec: AnalysisSpecV1) -> None:
        await require_repository(
            self.session, ctx.org_id, ctx.principal_id, spec.repository_id, write=True
        )
        stmt = analysis_specs.insert().values(
            org_id=ctx.org_id,
            id=spec.id,
            repository_id=spec.repository_id,
            snapshot_id=spec.snapshot_id,
            principal_id=spec.principal_id,
            config_json=spec.config.model_dump(mode="json"),
            config_hash=spec.config_hash,
            toolchain_hash=spec.toolchain_hash,
        )
        await self.session.execute(stmt)

    async def get_spec(
        self, ctx: TenantContext, repository_id: UUID, spec_id: UUID
    ) -> AnalysisSpecV1 | None:
        await authorize(self.session, ctx, False)
        await require_repository(self.session, ctx.org_id, ctx.principal_id, repository_id)
        stmt = select(analysis_specs).where(
            analysis_specs.c.org_id == ctx.org_id,
            analysis_specs.c.repository_id == repository_id,
            analysis_specs.c.id == spec_id,
        )
        row = (await self.session.execute(stmt)).mappings().one_or_none()
        if not row:
            return None
        return AnalysisSpecV1(
            org_id=row["org_id"],
            repository_id=row["repository_id"],
            snapshot_id=row["snapshot_id"],
            id=row["id"],
            principal_id=row["principal_id"],
            config=AnalysisConfigV1.model_validate(row["config_json"]),
            config_hash=row["config_hash"],
            toolchain_hash=row["toolchain_hash"],
        )

    async def get_report_by_run(
        self, ctx: TenantContext, snapshot_id: UUID, run_id: UUID
    ) -> ArchitectureReportV1 | None:
        await authorize(self.session, ctx, False)
        stmt = published_reports().where(
            structural_reports.c.org_id == ctx.org_id,
            structural_reports.c.snapshot_id == snapshot_id,
            structural_reports.c.run_id == run_id,
        )
        row = (await self.session.execute(stmt)).mappings().one_or_none()
        if not row:
            return None
        await require_repository(self.session, ctx.org_id, ctx.principal_id, row["repository_id"])
        return ArchitectureReportV1.model_validate(row["report_json"])

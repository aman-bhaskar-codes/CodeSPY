from uuid import uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.contracts.persistence import RunCreate, RunRecord
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, NotPublished, ScopeViolation
from app.domain.scope import RunRef, TenantContext
from app.models.tables import analysis_runs
from app.repositories.identity_repository import authorize


def run_filter(ref: RunRef) -> tuple[ColumnElement[bool], ...]:
    return (
        analysis_runs.c.org_id == ref.org_id,
        analysis_runs.c.snapshot_id == ref.snapshot_id,
        analysis_runs.c.id == ref.run_id,
    )


class AnalysisRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_run(self, ctx: TenantContext, data: RunCreate) -> RunRecord:
        await authorize(self.session, ctx, True)
        values = data.model_dump()
        values["config_hash"] = semantic_digest(data.config)
        row = (
            (
                await self.session.execute(
                    select(analysis_runs).where(
                        analysis_runs.c.org_id == ctx.org_id,
                        analysis_runs.c.request_key == data.request_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row:
            if any(row[k] != v for k, v in values.items()):
                raise DuplicateConflict("request key has different input")
            return RunRecord(**row)
        row = (
            (
                await self.session.execute(
                    insert(analysis_runs)
                    .values(org_id=ctx.org_id, id=uuid4(), **values)
                    .returning(analysis_runs)
                )
            )
            .mappings()
            .one()
        )
        return RunRecord(**row)

    async def get(
        self, ctx: TenantContext, ref: RunRef, *, lock: bool = False, published: bool = False
    ) -> RunRecord:
        await authorize(self.session, ctx, lock)
        if ref.org_id != ctx.org_id:
            raise ScopeViolation("foreign run scope")
        query = select(analysis_runs).where(*run_filter(ref))
        if lock:
            query = query.with_for_update()
        row = (await self.session.execute(query)).mappings().one_or_none()
        if not row:
            raise ScopeViolation("run unavailable in scope")
        if published and row["status"] not in ("completed", "partial"):
            raise NotPublished("run has no published result")
        return RunRecord(**row)

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.contracts.persistence import RunCreate, RunRecord
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, NotPublished, ScopeViolation
from app.domain.scope import RunRef, TenantContext
from app.models.tables import analysis_runs
from app.repositories.identity_repository import authorize


def run_columns() -> tuple[ColumnElement[Any], ...]:
    """Keep public Phase 1 reads valid after Phase 2 metadata is loaded."""
    return tuple(analysis_runs.c[name] for name in RunRecord.model_fields)


def run_filter(ref: RunRef) -> tuple[ColumnElement[bool], ...]:
    return (
        analysis_runs.c.org_id == ref.org_id,
        analysis_runs.c.snapshot_id == ref.snapshot_id,
        analysis_runs.c.id == ref.run_id,
    )


class AnalysisRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_run(
        self,
        ctx: TenantContext,
        data: RunCreate,
        *,
        run_id: UUID | None = None,
        worker_binding: tuple[UUID, UUID, int] | None = None,
    ) -> RunRecord:
        await authorize(self.session, ctx, True)
        values = data.model_dump()
        values["config_hash"] = semantic_digest(data.config)
        if worker_binding is not None:
            job_id, attempt_id, fence = worker_binding
            values.update(worker_job_id=job_id, worker_attempt_id=attempt_id, worker_fence=fence)
        row = (
            (
                await self.session.execute(
                    select(
                        *run_columns(),
                        *(analysis_runs.c[k] for k in values if k.startswith("worker_")),
                    ).where(
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
            return RunRecord(**{k: row[k] for k in RunRecord.model_fields})
        row = (
            (
                await self.session.execute(
                    insert(analysis_runs)
                    .values(org_id=ctx.org_id, id=run_id or uuid4(), **values)
                    .returning(*run_columns())
                )
            )
            .mappings()
            .one()
        )
        return RunRecord(**{k: row[k] for k in RunRecord.model_fields})

    async def get(
        self, ctx: TenantContext, ref: RunRef, *, lock: bool = False, published: bool = False
    ) -> RunRecord:
        await authorize(self.session, ctx, lock)
        if ref.org_id != ctx.org_id:
            raise ScopeViolation("foreign run scope")
        query = select(*run_columns()).where(*run_filter(ref))
        if lock:
            query = query.with_for_update()
        row = (await self.session.execute(query)).mappings().one_or_none()
        if not row:
            raise ScopeViolation("run unavailable in scope")
        if published and row["status"] not in ("completed", "partial"):
            raise NotPublished("run has no published result")
        return RunRecord(**{k: row[k] for k in RunRecord.model_fields})

from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.persistence import RepositoryCreate, RepositoryRecord
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import TenantContext
from app.models.tables import repositories
from app.repositories.identity_repository import authorize


class RepositoryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, ctx: TenantContext, data: RepositoryCreate) -> RepositoryRecord:
        await authorize(self.session, ctx, True)
        row = (
            (
                await self.session.execute(
                    select(repositories).where(
                        repositories.c.org_id == ctx.org_id,
                        repositories.c.repository_key == data.repository_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row:
            if any(row[k] != v for k, v in data.model_dump().items()):
                raise DuplicateConflict("repository key already has different metadata")
            return RepositoryRecord(**row)
        row = (
            (
                await self.session.execute(
                    insert(repositories)
                    .values(org_id=ctx.org_id, id=uuid4(), **data.model_dump())
                    .returning(repositories)
                )
            )
            .mappings()
            .one()
        )
        return RepositoryRecord(**row)

    async def get(self, ctx: TenantContext, id: UUID) -> RepositoryRecord:
        await authorize(self.session, ctx)
        row = (
            (
                await self.session.execute(
                    select(repositories).where(
                        repositories.c.org_id == ctx.org_id, repositories.c.id == id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if not row:
            raise ScopeViolation("repository unavailable in scope")
        return RepositoryRecord(**row)

    async def list(
        self, ctx: TenantContext, limit: int = 50, after: str | None = None
    ) -> list[RepositoryRecord]:
        await authorize(self.session, ctx)
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        query = select(repositories).where(repositories.c.org_id == ctx.org_id)
        if after:
            query = query.where(repositories.c.repository_key > after)
        return [
            RepositoryRecord(**row)
            for row in (
                await self.session.execute(
                    query.order_by(repositories.c.repository_key).limit(limit)
                )
            ).mappings()
        ]

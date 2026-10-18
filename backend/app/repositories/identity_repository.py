from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.tables import memberships, organizations, principals


class IdentityRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_fixture(self, slug: str) -> TenantContext:
        org_id, principal_id = uuid4(), uuid4()
        await self.session.execute(
            insert(organizations).values(id=org_id, slug=slug, display_name=slug)
        )
        await self.session.execute(
            insert(principals).values(
                id=principal_id,
                subject_key="local:" + str(principal_id),
                display_name="Local fixture",
            )
        )
        await self.session.execute(
            insert(memberships).values(org_id=org_id, principal_id=principal_id, role="owner")
        )
        return await self.resolve_context(org_id, principal_id)

    async def resolve_context(self, org_id: UUID, principal_id: UUID) -> TenantContext:
        role = await self.session.scalar(
            select(memberships.c.role).where(
                memberships.c.org_id == org_id, memberships.c.principal_id == principal_id
            )
        )
        if role is None:
            raise ScopeViolation("membership required")
        return TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole(role))


async def authorize(session: AsyncSession, ctx: TenantContext, write: bool = False) -> None:
    actual = await IdentityRepository(session).resolve_context(ctx.org_id, ctx.principal_id)
    if actual.role != ctx.role or (write and actual.role == OrgRole.VIEWER):
        raise ScopeViolation("membership role does not permit operation")

"""Fresh database authority, shared by requests and trusted worker transactions."""

from uuid import UUID

from app.db.time import database_now
from app.domain.errors import ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, repositories, source_snapshots
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def require_repository(
    session: AsyncSession,
    org_id: UUID,
    principal_id: UUID,
    repository_id: UUID,
    *,
    write: bool = False,
    lock: bool = False,
) -> TenantContext:
    grants = PHASE2_TABLES["repository_grants"]
    query = select(memberships.c.role).where(
        memberships.c.org_id == org_id, memberships.c.principal_id == principal_id
    )
    if lock:
        query = query.with_for_update()
    role = await session.scalar(query)
    if role is None or (write and role == "viewer"):
        raise ScopeViolation("repository unavailable")
    query = select(grants).where(
        grants.c.org_id == org_id,
        grants.c.repository_id == repository_id,
        grants.c.principal_id == principal_id,
    )
    if lock:
        query = query.with_for_update()
    grant = (await session.execute(query)).mappings().one_or_none()
    if (
        grant is None
        or grant["revoked_at"] is not None
        or (write and grant["permission"] != "write")
    ):
        raise ScopeViolation("repository unavailable")
    repo = (
        (
            await session.execute(
                select(repositories).where(
                    repositories.c.org_id == org_id, repositories.c.id == repository_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if repo is None:
        raise ScopeViolation("repository unavailable")
    if repo["source_kind"] == "github":
        binding = PHASE2_TABLES["github_repository_bindings"]
        user = PHASE2_TABLES["github_user_grants"]
        query = select(user).where(user.c.principal_id == principal_id)
        if lock:
            query = query.with_for_update()
        token = (await session.execute(query)).mappings().one_or_none()
        now = await database_now(session)
        if token is None or token["revoked_at"] is not None or token["expires_at"] <= now:
            raise ScopeViolation("repository unavailable")
        query = select(binding).where(
            binding.c.org_id == org_id, binding.c.repository_id == repository_id
        )
        if lock:
            query = query.with_for_update()
        active = (await session.execute(query)).mappings().one_or_none()
        if active is None or active["state"] != "active":
            raise ScopeViolation("repository unavailable")
    return TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole(role))


async def require_snapshot(
    session: AsyncSession,
    ctx: TenantContext,
    repository_id: UUID,
    snapshot_id: UUID,
    *,
    write: bool = False,
    lock: bool = False,
) -> TenantContext:
    """Bind sealed snapshot access to current repository authority in this transaction."""
    current = await require_repository(
        session, ctx.org_id, ctx.principal_id, repository_id, write=write, lock=lock
    )
    if lock:
        # Serialize first triage writes on an existing, API-lockable authority row.
        # Immutable snapshot/finding tables do not grant UPDATE to the API role.
        await session.scalar(
            select(repositories.c.id)
            .where(repositories.c.org_id == ctx.org_id, repositories.c.id == repository_id)
            .with_for_update()
        )
    query = select(source_snapshots.c.id).where(
        source_snapshots.c.org_id == ctx.org_id,
        source_snapshots.c.repository_id == repository_id,
        source_snapshots.c.id == snapshot_id,
        source_snapshots.c.state == "sealed",
    )
    if await session.scalar(query) is None:
        raise ScopeViolation("snapshot unavailable")
    return current


async def lock_scope(
    session: AsyncSession,
    org_id: UUID,
    principal_id: UUID,
    repository_id: UUID,
    snapshot_id: UUID | None = None,
    *,
    access: bool = True,
) -> TenantContext | None:
    limits = PHASE2_TABLES["org_work_limits"]
    found = await session.scalar(
        select(limits.c.org_id).where(limits.c.org_id == org_id).with_for_update()
    )
    if found is None:
        raise ScopeViolation("organization work limits unavailable")
    ctx = (
        await require_repository(
            session, org_id, principal_id, repository_id, write=True, lock=True
        )
        if access
        else None
    )
    repo = await session.scalar(
        select(repositories.c.id)
        .where(repositories.c.org_id == org_id, repositories.c.id == repository_id)
        .with_for_update()
    )
    if repo is None:
        raise ScopeViolation("repository unavailable")
    if snapshot_id is not None:
        if (
            session.bind is not None
            and session.bind.dialect.name == "postgresql"
            and await session.scalar(
                text("SELECT to_regproc('public.codespy_lock_scoped_snapshot')")
            )
        ):
            found = await session.scalar(
                text("SELECT public.codespy_lock_scoped_snapshot(:org,:actor,:repo,:snapshot)"),
                {
                    "org": org_id,
                    "actor": principal_id,
                    "repo": repository_id,
                    "snapshot": snapshot_id,
                },
            )
            if not found:
                raise ScopeViolation("snapshot unavailable")
            return ctx
        snap = await session.scalar(
            select(source_snapshots.c.id)
            .where(
                source_snapshots.c.org_id == org_id,
                source_snapshots.c.repository_id == repository_id,
                source_snapshots.c.id == snapshot_id,
                source_snapshots.c.state == "sealed",
            )
            .with_for_update()
        )
        if snap is None:
            raise ScopeViolation("snapshot unavailable")
    return ctx

"""Current sealed-snapshot and retained-source authority for retrieval channels."""

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.search import SearchScopeV1
from app.db.time import database_now
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.security.authorization import require_snapshot
from app.services.analysis_sources import locate_source


async def require_retrieval_source(session: AsyncSession, scope: SearchScopeV1) -> None:
    """Reject stale authority before returning indexed text or dispatching embeddings.

    Availability follows admitted database object records, not a physical storage probe.
    """
    await require_snapshot(
        session,
        TenantContext(org_id=scope.org_id, principal_id=scope.principal_id, role=OrgRole.MEMBER),
        scope.repository_id,
        scope.snapshot_id,
    )
    try:
        located = await locate_source(session, scope.org_id, scope.repository_id, scope.snapshot_id)
        objects = PHASE2_TABLES["object_records"]
        records = (
            (
                await session.execute(
                    select(objects).where(
                        objects.c.org_id == scope.org_id,
                        objects.c.repository_id == scope.repository_id,
                        objects.c.key.in_((located.archive.key, located.manifest.key)),
                        objects.c.state == "available",
                    )
                )
            )
            .mappings()
            .all()
        )
        now = await database_now(session)
        for kind, descriptor in (("source", located.archive), ("manifest", located.manifest)):
            matches = [
                row for row in records if row["kind"] == kind and row["key"] == descriptor.key
            ]
            if len(matches) != 1 or (
                matches[0]["sha256"] != descriptor.sha256
                or matches[0]["size_bytes"] != descriptor.size_bytes
                or matches[0]["version_id"] != descriptor.version_id
                or (matches[0]["expires_at"] is not None and matches[0]["expires_at"] <= now)
            ):
                raise ScopeViolation("retrieval retained input unavailable")
    except (NoResultFound, DuplicateConflict):
        raise ScopeViolation("retrieval source unavailable") from None

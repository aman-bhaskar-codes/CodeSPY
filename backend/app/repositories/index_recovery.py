"""Bounded recovery candidates; shared authorization still decides terminalization."""

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, repositories
from app.repositories.job_repository import jobs


def index_recovery_due() -> ColumnElement[bool]:
    grants = PHASE2_TABLES["repository_grants"]
    tokens = PHASE2_TABLES["github_user_grants"]
    bindings = PHASE2_TABLES["github_repository_bindings"]
    member = exists(
        select(memberships.c.org_id).where(
            memberships.c.org_id == jobs.c.org_id,
            memberships.c.principal_id == jobs.c.principal_id,
            memberships.c.role.in_(("owner", "admin", "member")),
        )
    ).correlate(jobs)
    grant = exists(
        select(grants.c.org_id).where(
            grants.c.org_id == jobs.c.org_id,
            grants.c.repository_id == jobs.c.repository_id,
            grants.c.principal_id == jobs.c.principal_id,
            grants.c.permission == "write",
            grants.c.revoked_at.is_(None),
        )
    ).correlate(jobs)
    token = exists(
        select(tokens.c.principal_id).where(
            tokens.c.principal_id == jobs.c.principal_id,
            tokens.c.revoked_at.is_(None),
            tokens.c.expires_at > func.clock_timestamp(),
        )
    ).correlate(jobs)
    binding = exists(
        select(bindings.c.org_id).where(
            bindings.c.org_id == jobs.c.org_id,
            bindings.c.repository_id == jobs.c.repository_id,
            bindings.c.state == "active",
        )
    ).correlate(jobs)
    github_lost = exists(
        select(repositories.c.id).where(
            repositories.c.org_id == jobs.c.org_id,
            repositories.c.id == jobs.c.repository_id,
            repositories.c.source_kind == "github",
            or_(~token, ~binding),
        )
    ).correlate(jobs)
    return and_(
        jobs.c.operation == "index_snapshot",
        or_(jobs.c.state == "cancelling", ~member, ~grant, github_lost),
    )

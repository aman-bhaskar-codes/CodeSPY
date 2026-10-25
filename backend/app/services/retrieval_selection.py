"""Authorized immutable lexical materialization selection shared by retrieval readers."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.contracts.indexing import IndexCoverageV1
from app.contracts.search import SearchScopeV1
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import search_indices, source_chunks
from app.models.index_tables import index_publication_receipts, retrieval_chunk_sets
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships
from app.services.retrieval_authority import require_retrieval_source


@dataclass(frozen=True)
class ResolvedChunkSetV1:
    chunk_set_id: UUID
    index_id: UUID
    config_hash: str
    materialization_hash: str
    coverage: IndexCoverageV1

    def response_fields(self) -> dict[str, Any]:
        return {
            "chunk_set_id": self.chunk_set_id,
            "config_hash": self.config_hash,
            "materialization_hash": self.materialization_hash,
            "coverage": self.coverage,
            "lexical_availability": "available",
        }


def _qualified(scope: SearchScopeV1) -> ColumnElement[bool]:
    p, i, r = retrieval_chunk_sets.c, search_indices.c, index_publication_receipts.c
    grants = PHASE2_TABLES["repository_grants"]
    authority = exists(
        select(1)
        .select_from(memberships.join(grants, memberships.c.org_id == grants.c.org_id))
        .where(
            memberships.c.org_id == scope.org_id,
            memberships.c.principal_id == scope.principal_id,
            grants.c.org_id == scope.org_id,
            grants.c.repository_id == scope.repository_id,
            grants.c.principal_id == scope.principal_id,
            grants.c.revoked_at.is_(None),
            grants.c.permission.in_(["read", "write", "admin"]),
        )
    )
    return and_(
        authority,
        p.org_id == scope.org_id,
        p.repository_id == scope.repository_id,
        p.snapshot_id == scope.snapshot_id,
        i.org_id == p.org_id,
        i.repository_id == p.repository_id,
        i.snapshot_id == p.snapshot_id,
        i.chunk_set_id == p.id,
        i.index_type == "lexical",
        i.state.in_(["ready", "partial_ready"]),
        i.sealed_at.is_not(None),
        r.org_id == p.org_id,
        r.repository_id == p.repository_id,
        r.snapshot_id == p.snapshot_id,
        r.chunk_set_id == p.id,
        r.index_id == i.id,
        r.materialization_hash == p.materialization_hash,
    )


async def resolve_lexical_set(
    session: AsyncSession, scope: SearchScopeV1, *, chunk_set_id: UUID | None = None
) -> ResolvedChunkSetV1 | None:
    await require_retrieval_source(session, scope)
    if (
        chunk_set_id is not None
        and scope.chunk_set_id is not None
        and chunk_set_id != scope.chunk_set_id
    ):
        raise ScopeViolation("Conflicting lexical materialization selection")
    requested = chunk_set_id or scope.chunk_set_id
    stmt = (
        select(retrieval_chunk_sets, search_indices.c.id.label("index_id"))
        .where(_qualified(scope))
        .order_by(retrieval_chunk_sets.c.created_at.desc(), retrieval_chunk_sets.c.id.desc())
        .limit(1)
    )
    if requested is not None:
        stmt = stmt.where(retrieval_chunk_sets.c.id == requested)
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        if requested is not None:
            raise ScopeViolation("Requested lexical materialization is unavailable")
        return None
    coverage = IndexCoverageV1.model_validate_json(
        canonical_bytes(row["manifest_json"]["coverage"])
    )
    if coverage.status != row["coverage"]:
        raise ScopeViolation("Lexical materialization coverage is inconsistent")
    return ResolvedChunkSetV1(
        row["id"], row["index_id"], row["config_hash"], row["materialization_hash"], coverage
    )


def selected_chunks(
    session: AsyncSession, scope: SearchScopeV1, resolved: ResolvedChunkSetV1
) -> ColumnElement[bool]:
    del session
    c, p = source_chunks.c, retrieval_chunk_sets.c
    qualified = exists(
        select(1)
        .select_from(retrieval_chunk_sets, search_indices, index_publication_receipts)
        .where(
            _qualified(scope),
            p.id == resolved.chunk_set_id,
            p.materialization_hash == resolved.materialization_hash,
            p.config_hash == resolved.config_hash,
            search_indices.c.id == resolved.index_id,
        )
    )
    return and_(
        c.org_id == scope.org_id,
        c.repository_id == scope.repository_id,
        c.snapshot_id == scope.snapshot_id,
        c.chunk_set_id == resolved.chunk_set_id,
        qualified,
    )


def validate_safe_content(row: Mapping[str, Any]) -> None:
    content = row["safe_content"]
    if (
        not isinstance(content, str)
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != row["safe_content_sha256"]
    ):
        raise ScopeViolation("Stored safe content digest differs from its materialization")

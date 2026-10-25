"""Synthetic channel ranking doubles; these do not qualify durable publication authority."""

import hashlib
from typing import Any
from uuid import UUID

import pytest
from app.contracts.indexing import IndexCoverageV1
from app.contracts.search import SearchScopeV1
from app.domain.scope import OrgRole, TenantContext
from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.services import retrieval_authority
from app.services.retrieval_selection import ResolvedChunkSetV1
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

SET_ID = UUID("6af7e7a4-96b4-4526-a0f5-0dc93fbc68c6")


def install_legacy_ranking(
    monkeypatch: pytest.MonkeyPatch, *, include_context: bool = False
) -> None:
    async def legacy_source(session: AsyncSession, scope: SearchScopeV1) -> None:
        # Keep the declared snapshot/source test doubles; skip production retention SQL only.
        await retrieval_authority.require_snapshot(
            session,
            TenantContext(
                org_id=scope.org_id, principal_id=scope.principal_id, role=OrgRole.MEMBER
            ),
            scope.repository_id,
            scope.snapshot_id,
        )
        await retrieval_authority.locate_source(
            session, scope.org_id, scope.repository_id, scope.snapshot_id
        )

    async def resolve(
        session: AsyncSession, scope: SearchScopeV1, *, chunk_set_id: UUID | None = None
    ) -> ResolvedChunkSetV1:
        assert chunk_set_id in (None, SET_ID)
        rows = (
            (
                await session.execute(
                    select(source_chunks).where(
                        source_chunks.c.org_id == scope.org_id,
                        source_chunks.c.snapshot_id == scope.snapshot_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            await session.execute(
                update(source_chunks)
                .where(source_chunks.c.org_id == scope.org_id, source_chunks.c.id == row["id"])
                .values(
                    chunk_set_id=SET_ID,
                    safe_content_sha256=hashlib.sha256(row["safe_content"].encode()).hexdigest(),
                )
            )
        for table in (search_indices, chunk_embeddings):
            if await session.run_sync(
                lambda s, table=table: s.bind.dialect.has_table(s.connection(), table.name)
            ):
                await session.execute(
                    update(table)
                    .where(table.c.org_id == scope.org_id, table.c.snapshot_id == scope.snapshot_id)
                    .values(chunk_set_id=SET_ID)
                )
        return ResolvedChunkSetV1(
            SET_ID,
            SET_ID,
            "a" * 64,
            "b" * 64,
            IndexCoverageV1(admitted_files=0, selected_files=0, processed_files=0),
        )

    def predicate(session: AsyncSession, scope: SearchScopeV1, resolved: ResolvedChunkSetV1) -> Any:
        return and_(
            source_chunks.c.org_id == scope.org_id,
            source_chunks.c.repository_id == scope.repository_id,
            source_chunks.c.snapshot_id == scope.snapshot_id,
            source_chunks.c.chunk_set_id == resolved.chunk_set_id,
        )

    for module in ("lexical_search", "dense_search", "hybrid_search"):
        monkeypatch.setattr(f"app.services.{module}.require_retrieval_source", legacy_source)
        monkeypatch.setattr(f"app.services.{module}.resolve_lexical_set", resolve)
    for module in ("lexical_search", "dense_search"):
        monkeypatch.setattr(f"app.services.{module}.selected_chunks", predicate)

    if include_context:
        monkeypatch.setattr("app.services.context_assembly.resolve_lexical_set", resolve)
        monkeypatch.setattr("app.services.context_assembly.selected_chunks", predicate)

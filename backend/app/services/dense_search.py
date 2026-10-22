"""Dense vector nearest-neighbor search service with strict multi-tenant scoping."""

import math
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.embedding.fake import FakeEmbeddingAdapter
from app.contracts.chunking import ChunkKind
from app.contracts.provider_budget import EmbeddingRequestV1
from app.contracts.search import (
    DenseSearchHitV1,
    DenseSearchRequestV1,
    DenseSearchResponseV1,
    IndexState,
    IndexType,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships
from app.ports.embedding import EmbeddingPort

grants = PHASE2_TABLES["repository_grants"]


class DenseSearchService:
    """Performs strictly scoped dense vector nearest-neighbor similarity searches."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        embedding_port: EmbeddingPort | None = None,
    ) -> None:
        self.session = session
        self.embedding_port = embedding_port or FakeEmbeddingAdapter()

    async def search_dense(self, request: DenseSearchRequestV1) -> DenseSearchResponseV1:
        """Execute a dense vector search within the authorized scope."""
        t0 = time.perf_counter()

        await self._verify_scope(
            request.scope.org_id,
            request.scope.repository_id,
            request.scope.principal_id,
        )

        # Step 2: Resolve active search index
        index_row = await self._resolve_index(request)
        if not index_row:
            return DenseSearchResponseV1(
                scope=request.scope,
                query=request.query,
                index_id=request.index_id or request.scope.snapshot_id,
                embedding_model="none",
                total_candidates=0,
                hits=(),
                execution_ms=round((time.perf_counter() - t0) * 1000, 2),
            )

        target_index_id = index_row["id"]
        model_id = index_row["embedding_model"] or "fake-v1"
        dimension = index_row["dimension"] or 1024

        # Step 3: Embed the query text
        query_req = EmbeddingRequestV1(
            text=request.query,
            model_id=model_id,
            dimensions=dimension,
            normalize=True,
        )
        query_resp = await self.embedding_port.embed(query_req)
        query_vec = query_resp.vector

        # Step 4: Load scoped embeddings joined with source_chunks
        stmt = (
            select(
                chunk_embeddings.c.chunk_id,
                chunk_embeddings.c.embedding_json,
                source_chunks.c.file_id,
                source_chunks.c.file_path,
                source_chunks.c.chunk_kind,
                source_chunks.c.symbol_name,
                source_chunks.c.qualified_name,
                source_chunks.c.start_byte,
                source_chunks.c.end_byte,
                source_chunks.c.start_line,
                source_chunks.c.end_line,
                source_chunks.c.safe_content,
                source_chunks.c.content_sha256,
            )
            .join(
                source_chunks,
                (chunk_embeddings.c.org_id == source_chunks.c.org_id)
                & (chunk_embeddings.c.chunk_id == source_chunks.c.id),
            )
            .where(
                chunk_embeddings.c.org_id == request.scope.org_id,
                chunk_embeddings.c.repository_id == request.scope.repository_id,
                chunk_embeddings.c.snapshot_id == request.scope.snapshot_id,
                chunk_embeddings.c.index_id == target_index_id,
            )
        )

        rows = (await self.session.execute(stmt)).mappings().all()
        scored_candidates: list[tuple[float, Any]] = []

        for row in rows:
            vec = row["embedding_json"]
            if not isinstance(vec, (list, tuple)):
                continue
            sim = self._cosine_similarity(query_vec, vec)
            norm_score = max(0.0, min(1.0, sim))
            if norm_score >= request.min_score:
                scored_candidates.append((norm_score, row))

        # Sort descending by similarity score
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        top_hits = scored_candidates[: request.limit]

        hits: list[DenseSearchHitV1] = []
        for score, row in top_hits:
            # Build short safe snippet
            content_str = str(row["safe_content"] or "")
            first_lines = "\n".join(content_str.splitlines()[:5])

            hits.append(
                DenseSearchHitV1(
                    chunk_id=row["chunk_id"],
                    file_id=row["file_id"],
                    file_path=row["file_path"],
                    chunk_kind=ChunkKind(row["chunk_kind"]),
                    symbol_name=row["symbol_name"],
                    qualified_name=row["qualified_name"],
                    start_byte=row["start_byte"],
                    end_byte=row["end_byte"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    score=round(score, 4),
                    snippet=first_lines,
                    content_sha256=row["content_sha256"],
                )
            )

        exec_ms = round((time.perf_counter() - t0) * 1000, 2)
        return DenseSearchResponseV1(
            scope=request.scope,
            query=request.query,
            index_id=target_index_id,
            embedding_model=model_id,
            total_candidates=len(scored_candidates),
            hits=tuple(hits),
            execution_ms=exec_ms,
        )

    async def _verify_scope(self, org_id: UUID, repository_id: UUID, principal_id: UUID) -> None:
        """Verify principal has active read/write grant on the repository."""
        mem_check = await self.session.execute(
            select(memberships.c.role).where(
                memberships.c.org_id == org_id,
                memberships.c.principal_id == principal_id,
            )
        )
        if not mem_check.first():
            raise ScopeViolation(f"Principal {principal_id} is not a member of org {org_id}")

        grant_check = await self.session.execute(
            select(grants.c.permission).where(
                grants.c.org_id == org_id,
                grants.c.repository_id == repository_id,
                grants.c.principal_id == principal_id,
                grants.c.permission.in_(["read", "write", "admin"]),
                grants.c.revoked_at.is_(None),
            )
        )
        if not grant_check.first():
            raise ScopeViolation(
                f"Principal {principal_id} lacks access to repository {repository_id}"
            )

    async def _resolve_index(self, request: DenseSearchRequestV1) -> Any | None:
        """Resolve the requested or latest ready dense/hybrid index."""
        if request.index_id is not None:
            stmt = select(search_indices).where(
                search_indices.c.org_id == request.scope.org_id,
                search_indices.c.id == request.index_id,
                search_indices.c.repository_id == request.scope.repository_id,
                search_indices.c.snapshot_id == request.scope.snapshot_id,
                search_indices.c.state.in_([str(IndexState.READY), str(IndexState.PARTIAL_READY)]),
            )
            return (await self.session.execute(stmt)).mappings().one_or_none()

        stmt = (
            select(search_indices)
            .where(
                search_indices.c.org_id == request.scope.org_id,
                search_indices.c.repository_id == request.scope.repository_id,
                search_indices.c.snapshot_id == request.scope.snapshot_id,
                search_indices.c.index_type.in_([str(IndexType.DENSE), str(IndexType.HYBRID)]),
                search_indices.c.state.in_([str(IndexState.READY), str(IndexState.PARTIAL_READY)]),
            )
            .order_by(search_indices.c.created_at.desc())
        )
        return (await self.session.execute(stmt)).mappings().first()

    @staticmethod
    def _cosine_similarity(u: Sequence[float], v: Sequence[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if len(u) != len(v) or not u:
            return 0.0
        dot = sum(a * b for a, b in zip(u, v, strict=True))
        norm_u = math.sqrt(sum(a * a for a in u))
        norm_v = math.sqrt(sum(b * b for b in v))
        if norm_u == 0.0 or norm_v == 0.0:
            return 0.0
        return dot / (norm_u * norm_v)

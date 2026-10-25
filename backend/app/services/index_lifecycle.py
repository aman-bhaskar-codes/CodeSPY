"""Scoped local index publication with external worker activation kept unavailable."""

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.chunking import SourceChunkV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.provider_budget import EmbeddingRequestV1
from app.contracts.search import IndexState, IndexType, SearchIndexRecordV1
from app.contracts.source_view import PrivacyPolicyV1
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import chunk_embeddings, search_indices
from app.models.index_tables import index_publication_receipts, retrieval_chunk_sets
from app.ports.embedding import EmbeddingPort
from app.security.authorization import require_repository
from app.services.provider_budget import ProviderBudgetService


class IndexLifecycleService:
    """Manages the staged lifecycle of lexical, dense, and hybrid search indices.

    State transitions:
      BUILDING -> SEALING -> READY / PARTIAL_READY / FAILED / STALE
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        embedding_port: EmbeddingPort | None = None,
        budget_service: ProviderBudgetService | None = None,
    ) -> None:
        self.session = session
        self.embedding_port = embedding_port
        self.budget_service = budget_service

    async def create_index(
        self,
        *,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        index_type: IndexType,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        dimension: int | None = None,
        total_chunks: int = 0,
        metadata_json: dict[str, Any] | None = None,
    ) -> SearchIndexRecordV1:
        """Create a new search index in BUILDING state."""
        index_id = uuid4()
        now = datetime.now(UTC)
        meta = metadata_json or {}

        stmt = search_indices.insert().values(
            org_id=org_id,
            id=index_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            index_type=str(index_type),
            state=str(IndexState.BUILDING),
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            dimension=dimension,
            total_chunks=total_chunks,
            indexed_chunks=0,
            failed_chunks=0,
            sealed_at=None,
            metadata_json=meta,
            created_at=now,
            updated_at=now,
        )
        await self.session.execute(stmt)
        await self.session.flush()

        return SearchIndexRecordV1(
            index_id=index_id,
            org_id=org_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            index_type=index_type,
            state=IndexState.BUILDING,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            dimension=dimension,
            total_chunks=total_chunks,
            indexed_chunks=0,
            failed_chunks=0,
            sealed_at=None,
            created_at=now,
            updated_at=now,
            metadata_json=meta,
        )

    async def get_index(self, org_id: UUID, index_id: UUID) -> SearchIndexRecordV1 | None:
        """Load an index by primary key."""
        stmt = select(search_indices).where(
            search_indices.c.org_id == org_id,
            search_indices.c.id == index_id,
        )
        row = (await self.session.execute(stmt)).mappings().one_or_none()
        if not row:
            return None
        return self._map_row(row)

    async def get_active_index(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        index_type: IndexType | None = None,
    ) -> SearchIndexRecordV1 | None:
        """Find the latest ready/partial_ready index for the given scope."""
        stmt = (
            select(search_indices)
            .where(
                search_indices.c.org_id == org_id,
                search_indices.c.repository_id == repository_id,
                search_indices.c.snapshot_id == snapshot_id,
                search_indices.c.state.in_([str(IndexState.READY), str(IndexState.PARTIAL_READY)]),
                search_indices.c.chunk_set_id.is_not(None),
                exists(
                    select(1)
                    .select_from(
                        retrieval_chunk_sets.join(
                            index_publication_receipts,
                            (index_publication_receipts.c.org_id == retrieval_chunk_sets.c.org_id)
                            & (
                                index_publication_receipts.c.chunk_set_id
                                == retrieval_chunk_sets.c.id
                            )
                            & (
                                index_publication_receipts.c.materialization_hash
                                == retrieval_chunk_sets.c.materialization_hash
                            ),
                        )
                    )
                    .where(
                        retrieval_chunk_sets.c.org_id == search_indices.c.org_id,
                        retrieval_chunk_sets.c.repository_id == search_indices.c.repository_id,
                        retrieval_chunk_sets.c.snapshot_id == search_indices.c.snapshot_id,
                        retrieval_chunk_sets.c.id == search_indices.c.chunk_set_id,
                    )
                ),
            )
            .order_by(search_indices.c.created_at.desc())
        )
        if index_type is not None:
            stmt = stmt.where(search_indices.c.index_type == str(index_type))

        row = (await self.session.execute(stmt)).mappings().first()
        if not row:
            return None
        return self._map_row(row)

    async def publish_embeddings_batch(
        self,
        *,
        org_id: UUID,
        index_id: UUID,
        chunks: Sequence[SourceChunkV1],
        lease: AttemptLeaseV2 | None = None,
        privacy_policy: PrivacyPolicyV1 | None = None,
        principal_id: UUID | None = None,
    ) -> dict[str, int]:
        """Validate and publish local batches; external worker publication is unavailable."""
        if not chunks:
            return {"indexed": 0, "failed": 0}

        if lease is not None:
            if lease.org_id != org_id:
                raise ScopeViolation("Lease org mismatch")
            # Jobs have no persisted index association, so GUCs cannot establish ownership.
            raise RuntimeError("Index publication requires a qualified worker binding")

        # Lock and check index state
        stmt = (
            select(search_indices)
            .where(
                search_indices.c.org_id == org_id,
                search_indices.c.id == index_id,
            )
            .with_for_update()
        )
        idx_row = (await self.session.execute(stmt)).mappings().one_or_none()
        if not idx_row:
            raise ScopeViolation(f"Index {index_id} not found")
        if idx_row["chunk_set_id"] is not None:
            raise ScopeViolation("Qualified immutable indexes cannot accept legacy batches")
        if idx_row["state"] != str(IndexState.BUILDING):
            raise RuntimeError(f"Index is in state {idx_row['state']}, cannot publish chunks")

        if any(
            chunk.org_id != idx_row["org_id"]
            or chunk.repository_id != idx_row["repository_id"]
            or chunk.snapshot_id != idx_row["snapshot_id"]
            for chunk in chunks
        ):
            raise ScopeViolation("Chunk scope differs from index scope")

        idx_type = IndexType(idx_row["index_type"])
        if idx_type == IndexType.LEXICAL:
            # Lexical index does not produce embeddings
            now = datetime.now(UTC)
            await self.session.execute(
                update(search_indices)
                .where(search_indices.c.org_id == org_id, search_indices.c.id == index_id)
                .values(
                    indexed_chunks=search_indices.c.indexed_chunks + len(chunks),
                    updated_at=now,
                )
            )
            return {"indexed": len(chunks), "failed": 0}

        if self.embedding_port is None:
            raise RuntimeError("Embedding adapter is not configured")

        provider_name = idx_row["embedding_provider"]
        model_id = idx_row["embedding_model"]
        dimension = idx_row["dimension"]
        if (
            not isinstance(provider_name, str)
            or not provider_name.strip()
            or not isinstance(model_id, str)
            or not model_id.strip()
            or not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
        ):
            raise ValueError("Dense index profile is incomplete")

        if provider_name != "fake":
            if privacy_policy is None or not privacy_policy.allow_cloud_providers:
                raise ScopeViolation("Privacy policy prohibits cloud embedding provider")
            if self.budget_service is None:
                raise ScopeViolation("External index publication requires a budget service")
            if principal_id is None:
                raise ScopeViolation("External index publication requires a principal")
            await require_repository(
                self.session, org_id, principal_id, idx_row["repository_id"], write=True
            )
            # No index job operation/binding exists to authorize durable external dispatch.
            raise RuntimeError("External index publication requires a qualified worker binding")

        indexed_count = 0
        failed_count = 0
        for chunk in chunks:
            try:
                response = await self.embedding_port.embed(
                    EmbeddingRequestV1(
                        text=chunk.safe_content,
                        provider=provider_name,
                        model_id=model_id,
                        dimensions=dimension,
                        normalize=True,
                    )
                )
                if (
                    response.provider != provider_name
                    or response.model_id != model_id
                    or len(response.vector) != dimension
                    or not all(math.isfinite(value) for value in response.vector)
                    or not any(response.vector)
                    or response.token_count < 0
                ):
                    raise ValueError("Embedding response differs from the index profile")
                await self.session.execute(
                    chunk_embeddings.insert().values(
                        org_id=org_id,
                        chunk_id=chunk.chunk_id,
                        index_id=index_id,
                        repository_id=idx_row["repository_id"],
                        snapshot_id=idx_row["snapshot_id"],
                        embedding_json=list(response.vector),
                        token_count=response.token_count,
                    )
                )
                indexed_count += 1
            except Exception:
                failed_count += 1

        now = datetime.now(UTC)
        # Update index progress
        await self.session.execute(
            update(search_indices)
            .where(search_indices.c.org_id == org_id, search_indices.c.id == index_id)
            .values(
                indexed_chunks=search_indices.c.indexed_chunks + indexed_count,
                failed_chunks=search_indices.c.failed_chunks + failed_count,
                updated_at=now,
            )
        )

        return {"indexed": indexed_count, "failed": failed_count}

    async def seal_index(
        self,
        org_id: UUID,
        index_id: UUID,
        *,
        tolerance_ratio: float = 0.05,
    ) -> SearchIndexRecordV1:
        """Seal the index and evaluate completion status (READY, PARTIAL_READY, or FAILED)."""
        stmt = (
            select(search_indices)
            .where(
                search_indices.c.org_id == org_id,
                search_indices.c.id == index_id,
            )
            .with_for_update()
        )
        row = (await self.session.execute(stmt)).mappings().one_or_none()
        if not row:
            raise ScopeViolation(f"Index {index_id} not found")

        if row["chunk_set_id"] is not None:
            raise ScopeViolation("Qualified immutable indexes cannot be resealed")

        total = max(row["total_chunks"], row["indexed_chunks"] + row["failed_chunks"])
        failed = row["failed_chunks"]
        indexed = row["indexed_chunks"]

        if total == 0 or (failed == 0 and indexed >= total):
            new_state = IndexState.READY
        elif total > 0 and (failed / total) <= tolerance_ratio:
            new_state = IndexState.PARTIAL_READY
        else:
            new_state = IndexState.FAILED

        now = datetime.now(UTC)
        await self.session.execute(
            update(search_indices)
            .where(search_indices.c.org_id == org_id, search_indices.c.id == index_id)
            .values(
                state=str(new_state),
                total_chunks=total,
                sealed_at=now,
                updated_at=now,
            )
        )

        # If sealed ready or partial_ready, mark previous indices for this snapshot and type STALE
        if new_state in (IndexState.READY, IndexState.PARTIAL_READY):
            await self.session.execute(
                update(search_indices)
                .where(
                    search_indices.c.org_id == org_id,
                    search_indices.c.repository_id == row["repository_id"],
                    search_indices.c.snapshot_id == row["snapshot_id"],
                    search_indices.c.index_type == row["index_type"],
                    search_indices.c.id != index_id,
                    search_indices.c.chunk_set_id.is_(None),
                    search_indices.c.state.in_(
                        [str(IndexState.READY), str(IndexState.PARTIAL_READY)]
                    ),
                )
                .values(state=str(IndexState.STALE), updated_at=now)
            )

        updated_row = (await self.session.execute(stmt)).mappings().one()
        return self._map_row(updated_row)

    def _map_row(self, row: Any) -> SearchIndexRecordV1:
        return SearchIndexRecordV1(
            index_id=row["id"],
            org_id=row["org_id"],
            repository_id=row["repository_id"],
            snapshot_id=row["snapshot_id"],
            index_type=IndexType(row["index_type"]),
            state=IndexState(row["state"]),
            embedding_provider=row["embedding_provider"],
            embedding_model=row["embedding_model"],
            dimension=row["dimension"],
            total_chunks=row["total_chunks"],
            indexed_chunks=row["indexed_chunks"],
            failed_chunks=row["failed_chunks"],
            sealed_at=row["sealed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata_json=row["metadata_json"] or {},
        )

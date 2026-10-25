"""Typed immutable indexing reads; callers own transactions and worker authority."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.indexing import IndexPublicationDraftV1, IndexReceiptV1, IndexSpecV1
from app.contracts.search import SearchScopeV1
from app.domain.canonical_json import canonical_bytes
from app.domain.scope import TenantContext
from app.models.index_tables import index_publication_receipts, index_specs, retrieval_chunk_sets
from app.security.authorization import lock_scope, require_repository


class IndexRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_spec(
        self, ctx: TenantContext, repository_id: UUID, spec_id: UUID
    ) -> IndexSpecV1 | None:
        await require_repository(self.session, ctx.org_id, ctx.principal_id, repository_id)
        payload = await self.session.scalar(
            select(index_specs.c.spec_json).where(
                index_specs.c.org_id == ctx.org_id,
                index_specs.c.repository_id == repository_id,
                index_specs.c.id == spec_id,
            )
        )
        return (
            None if payload is None else IndexSpecV1.model_validate_json(canonical_bytes(payload))
        )

    async def locked_set(self, scope: SearchScopeV1, config_hash: str) -> dict[str, Any] | None:
        """Serialize on shared scope locks without UPDATE privileges on immutable sets."""
        await lock_scope(
            self.session, scope.org_id, scope.principal_id, scope.repository_id, scope.snapshot_id
        )
        row = (
            (
                await self.session.execute(
                    select(retrieval_chunk_sets).where(
                        retrieval_chunk_sets.c.org_id == scope.org_id,
                        retrieval_chunk_sets.c.repository_id == scope.repository_id,
                        retrieval_chunk_sets.c.snapshot_id == scope.snapshot_id,
                        retrieval_chunk_sets.c.config_hash == config_hash,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else dict(row)

    async def insert_set(self, draft: IndexPublicationDraftV1) -> dict[str, Any]:
        """Insert a parent inside a worker publication; incomplete commits are rejected by SQL."""
        draft = IndexPublicationDraftV1.model_validate(draft.model_dump())
        raw = canonical_bytes(draft.manifest_payload())
        row = (
            (
                await self.session.execute(
                    retrieval_chunk_sets.insert()
                    .values(
                        org_id=draft.org_id,
                        id=draft.chunk_set_id(),
                        repository_id=draft.repository_id,
                        snapshot_id=draft.snapshot_id,
                        config_hash=draft.config_hash,
                        materialization_hash=draft.materialization_hash(),
                        manifest_hash=draft.materialization_hash(),
                        manifest_json=json.loads(raw),
                        canonical_text=raw.decode(),
                        chunk_count=len(draft.chunks),
                        safe_bytes=sum(len(c.safe_content.encode()) for c in draft.chunks),
                        coverage=draft.coverage.status,
                    )
                    .returning(retrieval_chunk_sets)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def get_receipt(
        self, org_id: UUID, job_id: UUID, attempt_id: UUID, fence: int
    ) -> IndexReceiptV1 | None:
        """Internal recovery read; this does not authorize a public request."""
        receipt, specs, sets = index_publication_receipts, index_specs, retrieval_chunk_sets
        row = (
            (
                await self.session.execute(
                    select(receipt, specs.c.config_hash, sets.c.manifest_json)
                    .join(
                        specs,
                        (specs.c.org_id == receipt.c.org_id) & (specs.c.id == receipt.c.spec_id),
                    )
                    .join(
                        sets,
                        (sets.c.org_id == receipt.c.org_id) & (sets.c.id == receipt.c.chunk_set_id),
                    )
                    .where(
                        receipt.c.org_id == org_id,
                        receipt.c.job_id == job_id,
                        receipt.c.attempt_id == attempt_id,
                        receipt.c.fence == fence,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        data = {key: row[key] for key in IndexReceiptV1.model_fields if key in row}
        data["index_spec_id"] = row["spec_id"]
        data["coverage"] = row["manifest_json"]["coverage"]
        return IndexReceiptV1.model_validate_json(canonical_bytes(data))

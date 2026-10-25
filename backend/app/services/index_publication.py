"""Atomic fenced lexical publication and durable commit-outcome recovery."""

import asyncio
import hashlib
from typing import Any
from uuid import uuid5

from sqlalchemy import bindparam, func, insert, literal_column, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.indexing import IndexPublicationDraftV1, IndexReceiptV1, IndexSpecV1
from app.contracts.jobs import AttemptLeaseV2
from app.db.time import database_now
from app.domain.canonical_json import canonical_bytes, load_json, semantic_digest
from app.domain.errors import DuplicateConflict, FinalizationOutcomeUnknown, StaleAttempt
from app.indexing.receipt import PreparedIndexReceipt
from app.models.chunk_tables import search_indices, source_chunks
from app.models.index_tables import (
    index_attempt_specs,
    index_jobs,
    index_publication_receipts,
    index_specs,
    retrieval_chunk_sets,
)
from app.models.phase2_tables import PHASE2_TABLES
from app.repositories.event_repository import append_event
from app.repositories.index_repository import IndexRepository
from app.repositories.job_repository import attempts, load_job, worker_context
from app.services.analysis_sources import locate_source
from app.services.finalization_budget import FinalizationBudget, rollback_or_invalidate
from app.services.index_jobs import make_spec
from app.services.jobs import JobService
from app.services.lexical_tokens import extract_path_tokens
from app.services.publication import PublicationExecutor


async def checkpoint(budget: FinalizationBudget) -> None:
    if budget.remaining() <= 0:
        raise TimeoutError("index finalization deadline exceeded")
    await asyncio.sleep(0)


async def current_input(
    session: AsyncSession, jobs: JobService, lease: AttemptLeaseV2
) -> tuple[dict[str, Any], dict[str, Any], IndexSpecV1]:
    """Acquire scope, job, attempt and retained-object locks in their shared order."""
    job, ctx = await jobs._locked(session, lease.org_id, lease.job_id)
    if ctx is None or job["operation"] != "index_snapshot" or lease.run_ref is not None:
        raise DuplicateConflict("index operation differs")
    attempt = await jobs._current(session, lease, job)
    row = (
        (
            await session.execute(
                select(index_specs).where(
                    index_specs.c.org_id == lease.org_id, index_specs.c.id == job["index_spec_id"]
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise DuplicateConflict("index specification unavailable")
    objects = PHASE2_TABLES["object_records"]
    records = (
        (
            await session.execute(
                select(objects)
                .where(
                    objects.c.org_id == lease.org_id,
                    objects.c.id.in_((row["source_object_id"], row["manifest_object_id"])),
                )
                .order_by(objects.c.id)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    spec = IndexSpecV1.model_validate_json(canonical_bytes(row["spec_json"]))
    now = await database_now(session)
    for kind, descriptor, identity in (
        ("source", spec.source, row["source_object_id"]),
        ("manifest", spec.manifest, row["manifest_object_id"]),
    ):
        record = next((r for r in records if r["id"] == identity), None)
        if record is None or (
            record["repository_id"] != spec.repository_id
            or record["kind"] != kind
            or record["state"] != "available"
            or record["key"] != descriptor.key
            or record["sha256"] != descriptor.sha256
            or record["size_bytes"] != descriptor.size_bytes
            or record["version_id"] != descriptor.version_id
            or (record["expires_at"] is not None and record["expires_at"] <= now)
        ):
            raise DuplicateConflict("retained index input unavailable")
    binding = (
        (
            await session.execute(
                select(index_attempt_specs).where(
                    index_attempt_specs.c.org_id == lease.org_id,
                    index_attempt_specs.c.id == lease.attempt_id,
                    index_attempt_specs.c.job_id == lease.job_id,
                    index_attempt_specs.c.fence == lease.fence,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if binding is None or (
        binding["spec_id"] != row["id"]
        or binding["spec_hash"] != spec.semantic_hash()
        or row["spec_hash"] != spec.semantic_hash()
        or row["config_hash"] != spec.config.semantic_hash()
        or job["config_hash"] != row["config_hash"]
        or (spec.org_id, spec.principal_id, spec.repository_id, spec.snapshot_id)
        != (lease.org_id, job["principal_id"], job["repository_id"], job["snapshot_id"])
    ):
        raise DuplicateConflict("index attempt specification differs")
    located = await locate_source(session, lease.org_id, spec.repository_id, spec.snapshot_id)
    if make_spec(ctx, spec.repository_id, spec.snapshot_id, spec.config, located) != spec:
        raise DuplicateConflict("current admitted index input differs")
    return job, attempt, spec


class IndexFinalizer:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        budget_seconds: float = 60,
        safety_seconds: float = 5,
        cleanup_seconds: float = 5,
    ):
        self.factory, self.jobs = factory, JobService(factory)
        self.safety_seconds, self.cleanup_seconds = safety_seconds, cleanup_seconds
        self.executor = PublicationExecutor(
            factory,
            budget_seconds=budget_seconds,
            safety_seconds=safety_seconds,
            cleanup_seconds=cleanup_seconds,
        )

    async def _stored(
        self, session: AsyncSession, lease: AttemptLeaseV2, draft_digest: str
    ) -> IndexReceiptV1:
        job = await load_job(session, lease.org_id, lease.job_id)
        result = await IndexRepository(session).get_receipt(
            lease.org_id, lease.job_id, lease.attempt_id, lease.fence
        )
        if (
            result is None
            or job["operation"] != "index_snapshot"
            or (
                job["state"] not in {"succeeded", "partial"}
                or job["current_attempt_id"] != lease.attempt_id
                or job["fence"] != lease.fence
                or job["result_index_id"] != result.index_id
                or job["index_spec_id"] != result.index_spec_id
            )
        ):
            raise StaleAttempt("index publication has no durable terminal receipt")
        if result.draft_digest != draft_digest:
            raise DuplicateConflict("index terminal publication differs")
        return result

    async def _durable(self, lease: AttemptLeaseV2, draft_digest: str) -> IndexReceiptV1:
        session = self.factory()
        try:
            async with asyncio.timeout(5), session.begin():
                await session.execute(text("SET LOCAL statement_timeout='2s'"))
                await session.execute(text("SET LOCAL lock_timeout='1s'"))
                return await self._stored(session, lease, draft_digest)
        except (StaleAttempt, DuplicateConflict):
            raise
        except Exception:
            raise FinalizationOutcomeUnknown(
                "index publication outcome requires recovery"
            ) from None
        finally:
            await rollback_or_invalidate(session, self.cleanup_seconds)

    async def finish(self, lease: AttemptLeaseV2, prepared: PreparedIndexReceipt) -> IndexReceiptV1:
        # No validation or serialization occurs before the aggregate executor starts.
        async def work(session: AsyncSession, budget: FinalizationBudget) -> IndexReceiptV1:
            if not isinstance(prepared, PreparedIndexReceipt):
                raise ValueError("index preparation receipt required")
            for chunk in prepared.draft.chunks:
                await checkpoint(budget)
                if (
                    hashlib.sha256(chunk.safe_content.encode()).hexdigest()
                    != chunk.safe_content_sha256
                ):
                    raise DuplicateConflict("index safe content differs")
            prepared.validate()
            await checkpoint(budget)
            hint = await load_job(session, lease.org_id, lease.job_id)
            if hint["state"] in {"succeeded", "partial"}:
                return await self._stored(session, lease, prepared.draft_digest)
            job, attempt, spec = await current_input(session, self.jobs, lease)
            now = await database_now(session)
            if (
                min(
                    (attempt["lease_expires_at"] - now).total_seconds(),
                    (job["deadline"] - now).total_seconds(),
                )
                < budget.remaining() + self.safety_seconds
            ):
                raise StaleAttempt("insufficient index finalization lease or deadline")
            if spec != prepared.spec:
                raise DuplicateConflict("index publication specification differs")
            await worker_context(session, lease)
            draft = prepared.draft
            repository = IndexRepository(session)
            # Scope locks already serialize this immutable config; do not reacquire earlier locks.
            parent = (
                (
                    await session.execute(
                        select(retrieval_chunk_sets).where(
                            retrieval_chunk_sets.c.org_id == spec.org_id,
                            retrieval_chunk_sets.c.repository_id == spec.repository_id,
                            retrieval_chunk_sets.c.snapshot_id == spec.snapshot_id,
                            retrieval_chunk_sets.c.config_hash == draft.config_hash,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if parent is None:
                await repository.insert_set(draft)
                await self._insert_chunks(session, draft, spec, budget)
                await session.execute(
                    insert(search_indices).values(
                        org_id=spec.org_id,
                        id=draft.lexical_index_id(),
                        repository_id=spec.repository_id,
                        snapshot_id=spec.snapshot_id,
                        index_type="lexical",
                        state="ready" if draft.coverage.status == "complete" else "partial_ready",
                        chunk_set_id=draft.chunk_set_id(),
                        total_chunks=len(draft.chunks),
                        indexed_chunks=len(draft.chunks),
                        failed_chunks=0,
                        sealed_at=func.clock_timestamp(),
                    )
                )
            else:
                await self._verify_existing(session, draft, dict(parent), budget)
            await checkpoint(budget)
            await session.execute(
                insert(index_publication_receipts).values(
                    org_id=spec.org_id,
                    repository_id=spec.repository_id,
                    snapshot_id=spec.snapshot_id,
                    job_id=lease.job_id,
                    attempt_id=lease.attempt_id,
                    fence=lease.fence,
                    spec_id=job["index_spec_id"],
                    spec_hash=prepared.spec_hash,
                    chunk_set_id=draft.chunk_set_id(),
                    index_id=draft.lexical_index_id(),
                    draft_digest=prepared.draft_digest,
                    materialization_hash=draft.materialization_hash(),
                    coverage=draft.coverage.status,
                )
            )
            updated = dict(
                (
                    await session.execute(
                        update(index_jobs)
                        .where(index_jobs.c.org_id == lease.org_id, index_jobs.c.id == lease.job_id)
                        .values(
                            state="succeeded" if draft.coverage.status == "complete" else "partial",
                            result_index_id=draft.lexical_index_id(),
                            finished_at=func.clock_timestamp(),
                        )
                        .returning(index_jobs)
                    )
                )
                .mappings()
                .one()
            )
            await session.execute(
                update(attempts)
                .where(
                    attempts.c.org_id == lease.org_id,
                    attempts.c.id == lease.attempt_id,
                    attempts.c.job_id == lease.job_id,
                )
                .values(state="succeeded", finished_at=func.clock_timestamp())
            )
            await append_event(
                session,
                updated,
                "index_completed" if draft.coverage.status == "complete" else "index_partial",
                {"stage": "publication"},
            )
            await checkpoint(budget)
            return await self._stored(session, lease, prepared.draft_digest)

        return await self.executor.execute(
            work, lambda: self._durable(lease, prepared.draft_digest)
        )

    async def _insert_chunks(
        self,
        session: AsyncSession,
        draft: IndexPublicationDraftV1,
        spec: IndexSpecV1,
        budget: FinalizationBudget,
    ) -> None:
        set_id = draft.chunk_set_id()
        for offset in range(0, len(draft.chunks), 64):
            await checkpoint(budget)
            rows = []
            for c in draft.chunks[offset : offset + 64]:
                provenance = c.model_dump(mode="json", exclude={"safe_content"})
                rows.append(
                    {
                        "org_id": spec.org_id,
                        "id": uuid5(set_id, semantic_digest(provenance)),
                        "repository_id": spec.repository_id,
                        "snapshot_id": spec.snapshot_id,
                        "file_id": c.file_id,
                        "file_path": c.file_path,
                        "chunk_index": c.chunk_index,
                        "chunk_kind": c.chunk_kind,
                        "start_byte": c.start_byte,
                        "end_byte": c.end_byte,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "content_sha256": c.content_sha256,
                        "byte_count": c.end_byte - c.start_byte,
                        "estimated_tokens": c.token_count,
                        "safe_content": c.safe_content,
                        "redaction_digest": c.redaction_digest,
                        "chunker_version": spec.config.chunker_version,
                        "chunk_set_id": set_id,
                        "_lexical_content": c.safe_content,
                        "_lexical_path": " ".join(extract_path_tokens(c.file_path)),
                        "safe_start_byte": c.safe_start_byte,
                        "safe_end_byte": c.safe_end_byte,
                        "safe_content_sha256": c.safe_content_sha256,
                        "provenance_json": provenance,
                    }
                )
            await session.execute(
                insert(source_chunks).values(
                    tsv=func.setweight(
                        func.to_tsvector("simple", bindparam("_lexical_path")),
                        literal_column("'B'::\"char\""),
                    ).concat(
                        func.setweight(
                            func.to_tsvector("simple", bindparam("_lexical_content")),
                            literal_column("'C'::\"char\""),
                        )
                    )
                ),
                rows,
            )

    async def _verify_existing(
        self,
        session: AsyncSession,
        draft: IndexPublicationDraftV1,
        parent: dict[str, Any],
        budget: FinalizationBudget,
    ) -> None:
        if parent["id"] != draft.chunk_set_id() or (
            parent["materialization_hash"] != draft.materialization_hash()
            or parent["manifest_json"]
            != load_json(canonical_bytes(draft.manifest_payload()).decode())
            or parent["chunk_count"] != len(draft.chunks)
            or parent["safe_bytes"] != sum(len(c.safe_content.encode()) for c in draft.chunks)
            or parent["coverage"] != draft.coverage.status
        ):
            raise DuplicateConflict("sealed index materialization differs")
        rows = (
            (
                await session.execute(
                    select(source_chunks)
                    .where(
                        source_chunks.c.org_id == draft.org_id,
                        source_chunks.c.chunk_set_id == parent["id"],
                    )
                    .order_by(
                        source_chunks.c.file_path,
                        source_chunks.c.file_id,
                        source_chunks.c.chunk_index,
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != len(draft.chunks):
            raise DuplicateConflict("sealed index chunk count differs")
        for row, chunk in zip(rows, draft.chunks, strict=True):
            await checkpoint(budget)
            if row["id"] != uuid5(
                parent["id"], semantic_digest(chunk.model_dump(exclude={"safe_content"}))
            ) or (
                row["provenance_json"] != chunk.model_dump(mode="json", exclude={"safe_content"})
                or row["safe_content"] != chunk.safe_content
                or row["safe_content_sha256"] != chunk.safe_content_sha256
                or row["tsv"] is None
                or row["org_id"] != draft.org_id
                or row["repository_id"] != draft.repository_id
                or row["snapshot_id"] != draft.snapshot_id
                or row["file_id"] != chunk.file_id
                or row["file_path"] != chunk.file_path
                or row["chunk_index"] != chunk.chunk_index
                or row["chunk_kind"] != chunk.chunk_kind
                or row["start_byte"] != chunk.start_byte
                or row["end_byte"] != chunk.end_byte
                or row["start_line"] != chunk.start_line
                or row["end_line"] != chunk.end_line
                or row["content_sha256"] != chunk.content_sha256
                or row["byte_count"] != chunk.end_byte - chunk.start_byte
                or row["estimated_tokens"] != chunk.token_count
                or row["redaction_digest"] != chunk.redaction_digest
                or row["chunker_version"] != "v1_safe_windows"
                or row["safe_start_byte"] != chunk.safe_start_byte
                or row["safe_end_byte"] != chunk.safe_end_byte
            ):
                raise DuplicateConflict("sealed index content differs")
        index = (
            (
                await session.execute(
                    select(search_indices).where(
                        search_indices.c.org_id == draft.org_id,
                        search_indices.c.id == draft.lexical_index_id(),
                        search_indices.c.chunk_set_id == parent["id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            index is None
            or index["index_type"] != "lexical"
            or (
                index["state"]
                != ("ready" if draft.coverage.status == "complete" else "partial_ready")
                or index["total_chunks"] != len(draft.chunks)
                or index["indexed_chunks"] != len(draft.chunks)
                or index["failed_chunks"] != 0
                or index["sealed_at"] is None
            )
        ):
            raise DuplicateConflict("sealed lexical index differs")

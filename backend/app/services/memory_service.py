"""Evidence-backed repository memory service for persistent findings and decisions."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryEntryV1,
    MemoryKind,
    MemoryQueryRequestV1,
    MemoryQueryResponseV1,
)
from app.contracts.search import SearchScopeV1
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.memory_tables import repo_memories
from app.models.phase2_tables import PHASE2_TABLES
from app.models.scan_tables import finding_occurrences
from app.models.tables import artifacts, source_files, source_snapshots
from app.security.authorization import require_repository
from app.services.retrieval_selection import (
    resolve_lexical_set,
    selected_chunks,
    validate_safe_content,
)


class MemoryService:
    """Manages creation and scoped retrieval of evidence-backed repository memories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_memory(self, request: MemoryCreateRequestV1) -> MemoryEntryV1:
        """Persist a new grounded memory entry with verified evidence pointers."""
        if (
            request.org_id is None
            or request.repository_id is None
            or request.snapshot_id is None
            or request.principal_id is None
        ):
            raise ValueError("org_id, repository_id, snapshot_id, and principal_id are required")

        await self._verify_scope(
            request.org_id,
            request.repository_id,
            request.principal_id,
            write=True,
        )
        if not await self.session.scalar(
            self._snapshot_available(request.org_id, request.repository_id, request.snapshot_id)
        ):
            raise ScopeViolation("Memory snapshot is unavailable in repository")
        for pointer in request.evidence_pointers:
            await self._verify_pointer(
                request.org_id,
                request.repository_id,
                request.snapshot_id,
                request.principal_id,
                pointer,
            )

        memory_id = uuid4()
        now = datetime.now(UTC)
        pointers_json = [p.model_dump(mode="json") for p in request.evidence_pointers]

        stmt = repo_memories.insert().values(
            org_id=request.org_id,
            id=memory_id,
            repository_id=request.repository_id,
            snapshot_id=request.snapshot_id,
            memory_kind=str(request.memory_kind),
            topic=request.topic,
            summary=request.summary,
            evidence_pointers=pointers_json,
            created_at=now,
            updated_at=now,
        )
        await self.session.execute(stmt)

        return MemoryEntryV1(
            memory_id=memory_id,
            org_id=request.org_id,
            repository_id=request.repository_id,
            snapshot_id=request.snapshot_id,
            memory_kind=request.memory_kind,
            topic=request.topic,
            summary=request.summary,
            evidence_pointers=request.evidence_pointers,
            created_at=now,
            updated_at=now,
        )

    async def query_memory(self, request: MemoryQueryRequestV1) -> MemoryQueryResponseV1:
        """Search repository memories matching query tokens within the authorized tenant scope."""
        if request.org_id is None or request.repository_id is None or request.principal_id is None:
            raise ValueError("org_id, repository_id, and principal_id are required")

        await self._verify_scope(
            request.org_id,
            request.repository_id,
            request.principal_id,
        )

        stmt = select(repo_memories).where(
            repo_memories.c.org_id == request.org_id,
            repo_memories.c.repository_id == request.repository_id,
            self._snapshot_available(
                request.org_id, request.repository_id, repo_memories.c.snapshot_id
            ).scalar_subquery(),
        )
        if request.snapshot_id is not None:
            owned = await self.session.scalar(
                select(source_snapshots.c.id).where(
                    source_snapshots.c.org_id == request.org_id,
                    source_snapshots.c.repository_id == request.repository_id,
                    source_snapshots.c.id == request.snapshot_id,
                )
            )
            if owned is None:
                raise ScopeViolation("Memory snapshot is outside repository")
            stmt = stmt.where(repo_memories.c.snapshot_id == request.snapshot_id)

        if request.memory_kind is not None:
            stmt = stmt.where(repo_memories.c.memory_kind == str(request.memory_kind))

        if request.topic is not None:
            stmt = stmt.where(repo_memories.c.topic == request.topic)

        query_terms = [t.lower() for t in request.query.split() if len(t) > 1]
        if query_terms:
            corpus = func.lower(repo_memories.c.topic + " " + repo_memories.c.summary)
            stmt = stmt.where(
                or_(*(corpus.contains(term, autoescape=True) for term in query_terms))
            )
        result = await self.session.stream(
            stmt.order_by(
                repo_memories.c.created_at.desc(), repo_memories.c.id.desc()
            ).execution_options(yield_per=100)
        )

        matched: list[MemoryEntryV1] = []

        try:
            async for r in result.mappings():
                ptrs = tuple(EvidencePointerV1(**p) for p in (r["evidence_pointers"] or []))
                try:
                    for pointer in ptrs:
                        await self._verify_pointer(
                            request.org_id,
                            request.repository_id,
                            r["snapshot_id"],
                            request.principal_id,
                            pointer,
                        )
                except ScopeViolation:
                    continue
                matched.append(
                    MemoryEntryV1(
                        memory_id=r["id"],
                        org_id=r["org_id"],
                        repository_id=r["repository_id"],
                        snapshot_id=r["snapshot_id"],
                        memory_kind=MemoryKind(r["memory_kind"]),
                        topic=r["topic"],
                        summary=r["summary"],
                        evidence_pointers=ptrs,
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                    )
                )

                if len(matched) >= request.limit:
                    break
        finally:
            await result.close()
        return MemoryQueryResponseV1(
            org_id=request.org_id,
            repository_id=request.repository_id,
            query=request.query,
            total_candidates=len(matched),
            items=tuple(matched),
        )

    def _snapshot_available(
        self, org_id: UUID, repository_id: UUID, snapshot_id: UUID | ColumnElement[UUID]
    ) -> Select[Any]:
        objects = PHASE2_TABLES["object_records"]
        return select(
            select(source_snapshots.c.id)
            .join(
                artifacts,
                (artifacts.c.org_id == source_snapshots.c.org_id)
                & (artifacts.c.repository_id == source_snapshots.c.repository_id)
                & (artifacts.c.id == source_snapshots.c.artifact_id),
            )
            .join(
                objects,
                (objects.c.org_id == artifacts.c.org_id)
                & (objects.c.repository_id == artifacts.c.repository_id)
                & (objects.c.key == artifacts.c.storage_key)
                & (objects.c.sha256 == artifacts.c.sha256)
                & (objects.c.size_bytes == artifacts.c.size_bytes),
            )
            .where(
                source_snapshots.c.org_id == org_id,
                source_snapshots.c.repository_id == repository_id,
                source_snapshots.c.id == snapshot_id,
                source_snapshots.c.state == "sealed",
                objects.c.kind == "source",
                objects.c.state == "available",
            )
            .correlate(repo_memories)
            .exists()
        )

    async def _verify_pointer(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        principal_id: UUID,
        pointer: EvidencePointerV1,
    ) -> None:
        file_id = await self.session.scalar(
            select(source_files.c.id).where(
                source_files.c.org_id == org_id,
                source_files.c.snapshot_id == snapshot_id,
                source_files.c.path == pointer.file_path,
            )
        )
        if file_id is None or pointer.end_line < pointer.start_line:
            raise ScopeViolation("Memory evidence file or range is invalid")
        if pointer.chunk_id is not None:
            if (
                pointer.chunk_set_id is None
                or pointer.materialization_hash is None
                or pointer.safe_content_sha256 is None
            ):
                raise ScopeViolation("Memory evidence requires qualified chunk provenance")
            scope = SearchScopeV1(
                org_id=org_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                principal_id=principal_id,
                chunk_set_id=pointer.chunk_set_id,
            )
            resolved = await resolve_lexical_set(
                self.session, scope, chunk_set_id=pointer.chunk_set_id
            )
            if resolved is None or pointer.materialization_hash != resolved.materialization_hash:
                raise ScopeViolation("Memory evidence materialization is unavailable")
            if pointer.config_hash is not None and pointer.config_hash != resolved.config_hash:
                raise ScopeViolation("Memory evidence configuration differs")
            result = await self.session.execute(
                select(source_chunks).where(
                    selected_chunks(self.session, scope, resolved),
                    source_chunks.c.org_id == org_id,
                    source_chunks.c.repository_id == repository_id,
                    source_chunks.c.snapshot_id == snapshot_id,
                    source_chunks.c.file_id == file_id,
                    source_chunks.c.id == pointer.chunk_id,
                    source_chunks.c.file_path == pointer.file_path,
                    source_chunks.c.start_line <= pointer.start_line,
                    source_chunks.c.end_line >= pointer.end_line,
                )
            )
            chunk = result.mappings().one_or_none()
            if chunk is None:
                raise ScopeViolation("Memory evidence chunk is outside scoped file range")
            validate_safe_content(dict(chunk))
            if chunk["safe_content_sha256"] != pointer.safe_content_sha256:
                raise ScopeViolation("Memory evidence safe-content digest differs")
            if pointer.sha256 is not None and pointer.sha256 != chunk["content_sha256"]:
                raise ScopeViolation("Memory evidence source digest differs")
        if pointer.finding_id is not None:
            finding = await self.session.scalar(
                select(finding_occurrences.c.id)
                .where(
                    finding_occurrences.c.org_id == org_id,
                    finding_occurrences.c.snapshot_id == snapshot_id,
                    finding_occurrences.c.file_id == file_id,
                    finding_occurrences.c.id == pointer.finding_id,
                )
                .limit(1)
            )
            if finding is None:
                raise ScopeViolation("Memory evidence finding is outside scoped file")

        if pointer.chunk_id is not None:
            # Recheck historical authority after all awaited evidence reads.
            current = await resolve_lexical_set(
                self.session, scope, chunk_set_id=pointer.chunk_set_id
            )
            if current is None or current.materialization_hash != pointer.materialization_hash:
                raise ScopeViolation("Memory evidence materialization became unavailable")

    async def _verify_scope(
        self, org_id: UUID, repository_id: UUID, principal_id: UUID, *, write: bool = False
    ) -> None:
        """Reuse current repository authority for HTTP, worker and MCP callers."""
        await require_repository(
            self.session, org_id, principal_id, repository_id, write=write, lock=True
        )

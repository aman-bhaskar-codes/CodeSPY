"""Evidence-backed repository memory service for persistent findings and decisions."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryEntryV1,
    MemoryKind,
    MemoryQueryRequestV1,
    MemoryQueryResponseV1,
)
from app.domain.errors import ScopeViolation
from app.models.memory_tables import repo_memories
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships

grants = PHASE2_TABLES["repository_grants"]


class MemoryService:
    """Manages creation and scoped retrieval of evidence-backed repository memories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_memory(self, request: MemoryCreateRequestV1) -> MemoryEntryV1:
        """Persist a new grounded memory entry with verified evidence pointers."""
        await self._verify_scope(
            request.org_id,
            request.repository_id,
            request.principal_id,
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
        await self._verify_scope(
            request.org_id,
            request.repository_id,
            request.principal_id,
        )

        stmt = select(repo_memories).where(
            repo_memories.c.org_id == request.org_id,
            repo_memories.c.repository_id == request.repository_id,
        )

        if request.memory_kind is not None:
            stmt = stmt.where(repo_memories.c.memory_kind == str(request.memory_kind))

        if request.topic is not None:
            stmt = stmt.where(repo_memories.c.topic == request.topic)

        result = await self.session.execute(stmt.order_by(repo_memories.c.created_at.desc()))
        rows = result.mappings().all()

        query_terms = [t.lower() for t in request.query.split() if len(t) > 1]
        matched: list[MemoryEntryV1] = []

        for r in rows:
            text_corpus = f"{r['topic']} {r['summary']}".lower()
            if not query_terms or any(term in text_corpus for term in query_terms):
                ptrs = tuple(EvidencePointerV1(**p) for p in (r["evidence_pointers"] or []))
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

        return MemoryQueryResponseV1(
            org_id=request.org_id,
            repository_id=request.repository_id,
            query=request.query,
            total_candidates=len(matched),
            items=tuple(matched),
        )

    async def _verify_scope(self, org_id: UUID, repository_id: UUID, principal_id: UUID) -> None:
        """Verify principal has active membership and repository access."""
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

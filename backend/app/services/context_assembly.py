"""Context packaging service with strict token budgeting, citation keys, and grounding."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.context import (
    ContextAssemblyRequestV1,
    ContextItemV1,
    ContextPacketV1,
)
from app.domain.canonical_json import semantic_digest
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships

grants = PHASE2_TABLES["repository_grants"]


class ContextAssemblyService:
    """Assembles grounded, budgeted context packets with citation keys and digest fencing."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def assemble_context(
        self,
        request: ContextAssemblyRequestV1,
    ) -> ContextPacketV1:
        """Assemble a budgeted ContextPacketV1 from candidate search hits."""
        # Step 1: Verify tenant scope & principal authorization
        await self._verify_scope(
            request.scope.org_id,
            request.scope.repository_id,
            request.scope.principal_id,
        )

        packet_id = uuid4()
        now = datetime.now(UTC)

        if not request.candidate_hits:
            empty_digest = semantic_digest(
                {
                    "packet_id": str(packet_id),
                    "scope": request.scope.model_dump(),
                    "query": request.query,
                    "items": [],
                }
            )
            return ContextPacketV1(
                packet_id=packet_id,
                scope=request.scope,
                query=request.query,
                system_preamble=request.system_preamble,
                items=(),
                total_tokens=0,
                total_bytes=0,
                items_count=0,
                omitted_count=0,
                packet_sha256=empty_digest,
                created_at=now,
            )

        # Step 2: Fetch full chunk content and metadata for candidates
        chunk_ids = [h.chunk_id for h in request.candidate_hits]
        stmt = select(
            source_chunks.c.id,
            source_chunks.c.safe_content,
            source_chunks.c.estimated_tokens,
            source_chunks.c.byte_count,
        ).where(
            source_chunks.c.org_id == request.scope.org_id,
            source_chunks.c.id.in_(chunk_ids),
        )
        chunk_map = {row["id"]: row for row in (await self.session.execute(stmt)).mappings().all()}

        # Step 3: Knapsack packing with token and byte budget limits
        budget = request.budget
        available_token_budget = max(0, budget.max_tokens - budget.reserved_overhead_tokens)
        max_bytes = budget.max_bytes

        total_tokens = 0
        total_bytes = 0
        accepted_items: list[ContextItemV1] = []
        omitted_count = 0
        file_counts: dict[str, int] = {}

        for hit in request.candidate_hits:
            chunk_row = chunk_map.get(hit.chunk_id)
            if not chunk_row:
                omitted_count += 1
                continue

            content = str(chunk_row["safe_content"] or "")
            tokens = int(chunk_row["estimated_tokens"])
            bytes_len = int(chunk_row["byte_count"])

            # File diversity limit: prevent single file monopolization
            if request.deduplicate_files and file_counts.get(hit.file_path, 0) >= 3:
                omitted_count += 1
                continue

            # Knapsack boundary checks
            if (
                total_tokens + tokens > available_token_budget
                or total_bytes + bytes_len > max_bytes
            ):
                omitted_count += 1
                continue

            # Accept item
            total_tokens += tokens
            total_bytes += bytes_len
            file_counts[hit.file_path] = file_counts.get(hit.file_path, 0) + 1

            citation = f"[{hit.file_path}#L{hit.start_line}-L{hit.end_line}]"
            accepted_items.append(
                ContextItemV1(
                    chunk_id=hit.chunk_id,
                    file_id=hit.file_id,
                    file_path=hit.file_path,
                    symbol_name=hit.symbol_name,
                    qualified_name=hit.qualified_name,
                    chunk_kind=hit.chunk_kind,
                    start_line=hit.start_line,
                    end_line=hit.end_line,
                    start_byte=hit.start_byte,
                    end_byte=hit.end_byte,
                    token_count=tokens,
                    byte_count=bytes_len,
                    safe_content=content,
                    citation_key=citation,
                    relevance_score=hit.score,
                    match_modalities=hit.match_modalities,
                )
            )

        # Step 4: Compute deterministic packet digest
        canonical_payload = {
            "packet_id": str(packet_id),
            "scope": request.scope.model_dump(),
            "query": request.query,
            "items": [item.model_dump() for item in accepted_items],
        }
        digest = semantic_digest(canonical_payload)

        return ContextPacketV1(
            packet_id=packet_id,
            scope=request.scope,
            query=request.query,
            system_preamble=request.system_preamble,
            items=tuple(accepted_items),
            total_tokens=total_tokens,
            total_bytes=total_bytes,
            items_count=len(accepted_items),
            omitted_count=omitted_count,
            packet_sha256=digest,
            created_at=now,
        )

    async def _verify_scope(self, org_id: UUID, repository_id: UUID, principal_id: UUID) -> None:
        """Verify principal has active membership and repository read grant."""
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

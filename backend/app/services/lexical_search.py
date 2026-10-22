"""Lexical and exact code retrieval engine with weighted PostgreSQL FTS and dialect safety."""

import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.search import (
    ExactSearchRequestV1,
    ExactSearchType,
    LexicalSearchRequestV1,
    SearchHitV1,
    SearchMode,
    SearchResponseV1,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships
from app.services.lexical_tokens import (
    build_search_terms,
    extract_path_tokens,
    split_identifier_subwords,
)


class LexicalSearchService:
    """Provides verified exact and full-text code retrieval scoped to snapshots and members."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def index_chunks(self, chunks: Sequence[SourceChunkV1]) -> int:
        """Batch index source chunks into source_chunks table with weighted tsvectors."""
        if not chunks:
            return 0

        # Detect dialect: PostgreSQL vs SQLite
        dialect_name = self.s.bind.dialect.name if self.s.bind else "postgresql"

        count = 0
        for c in chunks:
            # Prepare weighted text representations
            a_tokens = [c.symbol_name or "", c.qualified_name or ""]
            b_tokens = extract_path_tokens(c.file_path)
            if c.symbol_name:
                b_tokens.extend(split_identifier_subwords(c.symbol_name))

            a_text = " ".join(t for t in a_tokens if t)
            b_text = " ".join(t for t in b_tokens if t)
            c_text = c.safe_content

            tsv_expr: Any = None
            if dialect_name == "postgresql":
                # Weighted tsvector in PostgreSQL: A=symbols, B=paths/identifiers, C=content
                tsv_expr = (
                    func.setweight(func.to_tsvector("simple", a_text), "A")
                    .concat(func.setweight(func.to_tsvector("simple", b_text), "B"))
                    .concat(func.setweight(func.to_tsvector("simple", c_text), "C"))
                )
            else:
                # Fallback for SQLite / test environments
                tsv_expr = f"{a_text} {b_text} {c_text}"

            await self.s.execute(
                source_chunks.insert().values(
                    org_id=c.org_id,
                    id=c.chunk_id,
                    repository_id=c.repository_id,
                    snapshot_id=c.snapshot_id,
                    file_id=c.file_id,
                    file_path=c.file_path,
                    chunk_index=c.chunk_index,
                    chunk_kind=c.chunk_kind.value,
                    symbol_name=c.symbol_name,
                    qualified_name=c.qualified_name,
                    cpg_node_id=c.cpg_node_id,
                    start_byte=c.start_byte,
                    end_byte=c.end_byte,
                    start_line=c.start_line,
                    end_line=c.end_line,
                    content_sha256=c.content_sha256,
                    byte_count=c.byte_count,
                    estimated_tokens=c.token_count,
                    safe_content=c.safe_content,
                    redaction_digest=c.redaction_digest,
                    chunker_version=c.chunker_version,
                    tsv=tsv_expr,
                )
            )
            count += 1
        return count

    async def clear_snapshot_chunks(
        self, org_id: UUID, snapshot_id: UUID, chunker_version: str | None = None
    ) -> int:
        """Clear indexed chunks for a snapshot to support clean reindexing."""
        stmt = delete(source_chunks).where(
            source_chunks.c.org_id == org_id,
            source_chunks.c.snapshot_id == snapshot_id,
        )
        if chunker_version:
            stmt = stmt.where(source_chunks.c.chunker_version == chunker_version)
        result = await self.s.execute(stmt)
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def search_exact(self, request: ExactSearchRequestV1) -> SearchResponseV1:
        """Search exact file paths, path segments, and symbol identifiers."""
        t0 = time.perf_counter()
        if request.scope is None:
            raise ValueError("scope is required for search")
        await self._verify_scope(request.scope)

        q = request.query.strip()
        sc = source_chunks.c
        filters = [
            sc.org_id == request.scope.org_id,
            sc.snapshot_id == request.scope.snapshot_id,
        ]

        st = request.search_type
        if st == ExactSearchType.FILE_PATH:
            match_cond = or_(sc.file_path == q, sc.file_path.endswith(f"/{q}"))
        elif st == ExactSearchType.PATH_SEGMENT:
            match_cond = sc.file_path.ilike(f"%{q}%")
        elif st == ExactSearchType.SYMBOL:
            match_cond = or_(sc.symbol_name == q, sc.symbol_name.ilike(q))
        elif st == ExactSearchType.QUALIFIED_NAME:
            match_cond = or_(sc.qualified_name == q, sc.qualified_name.ilike(q))
        else:
            # Unified exact search: check symbol, qualified_name, exact path, or path segment
            match_cond = or_(
                sc.symbol_name == q,
                sc.symbol_name.ilike(q),
                sc.qualified_name == q,
                sc.file_path == q,
                sc.file_path.endswith(f"/{q}"),
                sc.file_path.ilike(f"%{q}%"),
            )

        query_stmt = (
            select(source_chunks)
            .where(*filters, match_cond)
            .order_by(
                # Prioritize exact symbol match first, then exact path match
                (sc.symbol_name == q).desc(),
                (sc.file_path == q).desc(),
                sc.file_path.asc(),
                sc.start_byte.asc(),
                sc.id.asc(),
            )
            .limit(request.limit)
        )

        rows = (await self.s.execute(query_stmt)).mappings().all()

        hits: list[SearchHitV1] = []
        for r in rows:
            reasons = []
            if r["symbol_name"] == q:
                reasons.append("exact_symbol")
            elif r["symbol_name"] and r["symbol_name"].lower() == q.lower():
                reasons.append("case_insensitive_symbol")
            if r["file_path"] == q or r["file_path"].endswith(f"/{q}"):
                reasons.append("exact_path")
            elif q in r["file_path"]:
                reasons.append("path_segment")
            if r["qualified_name"] == q:
                reasons.append("exact_qualified_name")

            snippet = self._generate_snippet(str(r["safe_content"]), [q])
            hits.append(
                SearchHitV1(
                    chunk_id=r["id"],
                    file_id=r["file_id"],
                    file_path=str(r["file_path"]),
                    chunk_kind=ChunkKind(r["chunk_kind"]),
                    symbol_name=r["symbol_name"],
                    qualified_name=r["qualified_name"],
                    start_byte=r["start_byte"],
                    end_byte=r["end_byte"],
                    start_line=r["start_line"],
                    end_line=r["end_line"],
                    score=1.0,
                    snippet=snippet,
                    content_sha256=str(r["content_sha256"]),
                    match_reasons=tuple(reasons) or ("exact_match",),
                )
            )

        elapsed = (time.perf_counter() - t0) * 1000.0
        return SearchResponseV1(
            scope=request.scope,
            mode=SearchMode.EXACT,
            query=request.query,
            total_candidates=len(hits),
            hits=tuple(hits),
            coverage_status="complete" if hits else "no_results",
            execution_ms=round(elapsed, 2),
        )

    async def search_lexical(self, request: LexicalSearchRequestV1) -> SearchResponseV1:
        """Full-text lexical search using PostgreSQL tsvector or dialect fallback."""
        t0 = time.perf_counter()
        if request.scope is None:
            raise ValueError("scope is required for search")
        await self._verify_scope(request.scope)

        dialect_name = self.s.bind.dialect.name if self.s.bind else "postgresql"
        terms = build_search_terms(request.query)
        if not terms:
            return SearchResponseV1(
                scope=request.scope,
                mode=SearchMode.LEXICAL,
                query=request.query,
                total_candidates=0,
                hits=(),
                coverage_status="no_results",
                execution_ms=0.0,
            )

        sc = source_chunks.c
        scope_filters = [
            sc.org_id == request.scope.org_id,
            sc.snapshot_id == request.scope.snapshot_id,
        ]

        hits: list[SearchHitV1] = []

        if dialect_name == "postgresql":
            # Parameterized plainto_tsquery over simple dictionary
            # Join search terms with ' | ' for disjunctive query or plainto_tsquery
            safe_query_str = " ".join(terms)
            query_ts = func.plainto_tsquery("simple", safe_query_str)
            rank_expr = func.ts_rank_cd(sc.tsv, query_ts)

            stmt = (
                select(source_chunks, rank_expr.label("rank"))
                .where(*scope_filters, sc.tsv.op("@@")(query_ts))
                .order_by(text("rank DESC"), sc.file_path.asc(), sc.start_byte.asc(), sc.id.asc())
                .limit(request.limit)
            )
            rows = (await self.s.execute(stmt)).mappings().all()
            for r in rows:
                snippet = self._generate_snippet(str(r["safe_content"]), terms)
                hits.append(
                    SearchHitV1(
                        chunk_id=r["id"],
                        file_id=r["file_id"],
                        file_path=str(r["file_path"]),
                        chunk_kind=ChunkKind(r["chunk_kind"]),
                        symbol_name=r["symbol_name"],
                        qualified_name=r["qualified_name"],
                        start_byte=r["start_byte"],
                        end_byte=r["end_byte"],
                        start_line=r["start_line"],
                        end_line=r["end_line"],
                        score=float(r["rank"]),
                        snippet=snippet,
                        content_sha256=str(r["content_sha256"]),
                        match_reasons=("fts_tsvector",),
                    )
                )
        else:
            # Fallback for SQLite and unit test execution
            like_conditions = []
            for t in terms:
                like_pattern = f"%{t}%"
                like_conditions.append(
                    or_(
                        sc.symbol_name.ilike(like_pattern),
                        sc.qualified_name.ilike(like_pattern),
                        sc.file_path.ilike(like_pattern),
                        sc.safe_content.ilike(like_pattern),
                    )
                )

            stmt = (
                select(source_chunks)
                .where(*scope_filters, or_(*like_conditions))
                .limit(request.limit * 2)
            )
            raw_rows = (await self.s.execute(stmt)).mappings().all()

            # Rank candidates in Python for SQLite test consistency
            scored_rows = []
            for r in raw_rows:
                score = 0.0
                reasons = []
                sym = (r["symbol_name"] or "").lower()
                qual = (r["qualified_name"] or "").lower()
                path = r["file_path"].lower()
                content = r["safe_content"].lower()

                for t in terms:
                    tl = t.lower()
                    if tl == sym:
                        score += 3.0
                        reasons.append(f"exact_symbol:{t}")
                    elif tl in sym:
                        score += 1.5
                        reasons.append(f"partial_symbol:{t}")
                    if tl in qual:
                        score += 1.0
                    if tl in path:
                        score += 1.2
                        reasons.append(f"path_match:{t}")
                    if tl in content:
                        score += 0.5

                if score > 0.0:
                    scored_rows.append((score, r, reasons))

            scored_rows.sort(
                key=lambda x: (
                    -x[0],
                    x[1]["file_path"],
                    x[1]["start_byte"],
                    str(x[1]["id"]),
                )
            )

            for score, r, reasons in scored_rows[: request.limit]:
                snippet = self._generate_snippet(str(r["safe_content"]), terms)
                hits.append(
                    SearchHitV1(
                        chunk_id=r["id"],
                        file_id=r["file_id"],
                        file_path=str(r["file_path"]),
                        chunk_kind=ChunkKind(r["chunk_kind"]),
                        symbol_name=r["symbol_name"],
                        qualified_name=r["qualified_name"],
                        start_byte=r["start_byte"],
                        end_byte=r["end_byte"],
                        start_line=r["start_line"],
                        end_line=r["end_line"],
                        score=score,
                        snippet=snippet,
                        content_sha256=str(r["content_sha256"]),
                        match_reasons=tuple(reasons) or ("keyword_match",),
                    )
                )

        elapsed = (time.perf_counter() - t0) * 1000.0
        return SearchResponseV1(
            scope=request.scope,
            mode=SearchMode.LEXICAL,
            query=request.query,
            total_candidates=len(hits),
            hits=tuple(hits),
            coverage_status="complete" if hits else "no_results",
            execution_ms=round(elapsed, 2),
        )

    async def _verify_scope(self, scope: SearchScopeV1) -> None:
        """Enforce tenant membership and repository grant."""
        role = await self.s.scalar(
            select(memberships.c.role).where(
                memberships.c.org_id == scope.org_id,
                memberships.c.principal_id == scope.principal_id,
            )
        )
        if role is None:
            raise ScopeViolation("Principal is not a member of the organization")

        grants = PHASE2_TABLES["repository_grants"]
        grant = (
            (
                await self.s.execute(
                    select(grants).where(
                        grants.c.org_id == scope.org_id,
                        grants.c.repository_id == scope.repository_id,
                        grants.c.principal_id == scope.principal_id,
                        grants.c.revoked_at.is_(None),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if grant is None:
            raise ScopeViolation("Repository grant is missing or revoked")

    def _generate_snippet(self, content: str, terms: Sequence[str], max_lines: int = 5) -> str:
        """Extract a relevant snippet of code around matching terms."""
        lines = content.splitlines()
        if not lines:
            return ""
        if len(lines) <= max_lines:
            return content

        # Find the line with the most term hits
        best_line = 0
        best_matches = 0
        lower_terms = [t.lower() for t in terms if t]

        for i, line in enumerate(lines):
            ll = line.lower()
            matches = sum(1 for t in lower_terms if t in ll)
            if matches > best_matches:
                best_matches = matches
                best_line = i

        start_line = max(0, best_line - 2)
        end_line = min(len(lines), start_line + max_lines)
        return "\n".join(lines[start_line:end_line])

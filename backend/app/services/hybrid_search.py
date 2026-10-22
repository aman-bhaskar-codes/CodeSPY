"""Hybrid search combining exact symbol, lexical full-text, and dense vector retrieval."""

import time
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.chunking import ChunkKind
from app.contracts.search import (
    DenseSearchRequestV1,
    ExactSearchRequestV1,
    HybridSearchHitV1,
    HybridSearchRequestV1,
    HybridSearchResponseV1,
    LexicalSearchRequestV1,
)
from app.models.phase2_tables import PHASE2_TABLES
from app.services.dense_search import DenseSearchService
from app.services.lexical_search import LexicalSearchService

grants = PHASE2_TABLES["repository_grants"]


class HybridSearchService:
    """Combines exact, lexical, and dense channels using Reciprocal Rank Fusion (RRF)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        lexical_service: LexicalSearchService | None = None,
        dense_service: DenseSearchService | None = None,
    ) -> None:
        self.session = session
        self.lexical_service = lexical_service or LexicalSearchService(session)
        self.dense_service = dense_service or DenseSearchService(session)

    async def search_hybrid(self, request: HybridSearchRequestV1) -> HybridSearchResponseV1:
        t0 = time.perf_counter()
        if request.scope is None:
            raise ValueError("scope is required for hybrid search")

        # Step 1: Execute retrieval channels
        exact_req = ExactSearchRequestV1(
            scope=request.scope,
            query=request.query,
            limit=request.exact_limit,
        )
        lexical_req = LexicalSearchRequestV1(
            scope=request.scope,
            query=request.query,
            limit=request.lexical_limit,
        )
        dense_req = DenseSearchRequestV1(
            scope=request.scope,
            query=request.query,
            index_id=request.dense_index_id,
            limit=request.dense_limit,
        )

        exact_res = await self.lexical_service.search_exact(exact_req)
        lexical_res = await self.lexical_service.search_lexical(lexical_req)
        dense_res = await self.dense_service.search_dense(dense_req)

        # Step 2: Compute Reciprocal Rank Fusion scores
        k = request.rrf_k
        w_exact = request.weights.get("exact", 1.5)
        w_lexical = request.weights.get("lexical", 1.0)
        w_dense = request.weights.get("dense", 1.0)

        scores: dict[UUID, float] = {}
        modalities: dict[UUID, list[str]] = {}
        ranks: dict[UUID, dict[str, int]] = {}
        chunk_data: dict[UUID, Any] = {}

        # Channel 1: Exact matches
        for r, exact_hit in enumerate(exact_res.hits, start=1):
            cid = exact_hit.chunk_id
            delta = w_exact / (k + r)
            scores[cid] = scores.get(cid, 0.0) + delta
            modalities.setdefault(cid, []).append("exact")
            ranks.setdefault(cid, {})["exact"] = r
            if cid not in chunk_data:
                chunk_data[cid] = exact_hit

        # Channel 2: Lexical matches
        for r, lexical_hit in enumerate(lexical_res.hits, start=1):
            cid = lexical_hit.chunk_id
            delta = w_lexical / (k + r)
            scores[cid] = scores.get(cid, 0.0) + delta
            if "lexical" not in modalities.setdefault(cid, []):
                modalities[cid].append("lexical")
            ranks.setdefault(cid, {})["lexical"] = r
            if cid not in chunk_data:
                chunk_data[cid] = lexical_hit

        # Channel 3: Dense vector matches
        for r, dense_hit in enumerate(dense_res.hits, start=1):
            cid = dense_hit.chunk_id
            delta = w_dense / (k + r)
            scores[cid] = scores.get(cid, 0.0) + delta
            if "dense" not in modalities.setdefault(cid, []):
                modalities[cid].append("dense")
            ranks.setdefault(cid, {})["dense"] = r
            if cid not in chunk_data:
                chunk_data[cid] = dense_hit

        # Step 3: Sort by fused score
        ranked_candidates = sorted(
            scores.items(),
            key=lambda x: (x[1], 1 if getattr(chunk_data[x[0]], "symbol_name", None) else 0),
            reverse=True,
        )

        # Step 4: Deduplicate and near-duplicate suppression
        final_hits: list[HybridSearchHitV1] = []
        seen_spans: dict[str, list[tuple[int, int, ChunkKind]]] = {}

        for cid, score in ranked_candidates:
            hit = chunk_data[cid]
            file_path = hit.file_path
            sb = hit.start_byte
            eb = hit.end_byte
            kind = hit.chunk_kind

            # Near-duplicate suppression for overlapping chunks within the same file
            is_subsumed = False
            for prev_sb, prev_eb, prev_kind in seen_spans.get(file_path, []):
                # If existing span fully envelops this span and is more specific or equal
                if (
                    prev_sb <= sb
                    and prev_eb >= eb
                    and prev_kind in (ChunkKind.FUNCTION, ChunkKind.CLASS)
                    and kind in (ChunkKind.WINDOW, ChunkKind.FILE)
                ):
                    is_subsumed = True
                    break

            if is_subsumed:
                continue

            seen_spans.setdefault(file_path, []).append((sb, eb, kind))

            final_hits.append(
                HybridSearchHitV1(
                    chunk_id=cid,
                    file_id=hit.file_id,
                    file_path=hit.file_path,
                    chunk_kind=hit.chunk_kind,
                    symbol_name=hit.symbol_name,
                    qualified_name=hit.qualified_name,
                    start_byte=hit.start_byte,
                    end_byte=hit.end_byte,
                    start_line=hit.start_line,
                    end_line=hit.end_line,
                    score=round(score, 6),
                    snippet=hit.snippet,
                    content_sha256=hit.content_sha256,
                    match_modalities=tuple(modalities[cid]),
                    modality_ranks=ranks[cid],
                )
            )
            if len(final_hits) >= request.limit:
                break

        exec_ms = round((time.perf_counter() - t0) * 1000, 2)
        return HybridSearchResponseV1(
            scope=request.scope,
            query=request.query,
            total_candidates=len(scores),
            hits=tuple(final_hits),
            execution_ms=exec_ms,
        )

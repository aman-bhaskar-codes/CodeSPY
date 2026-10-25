"""Selection boundary tests; database publication qualification runs separately."""

import hashlib
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.contracts.search import SearchScopeV1
from app.domain.errors import ScopeViolation
from app.services.retrieval_selection import resolve_lexical_set, validate_safe_content


def scope() -> SearchScopeV1:
    return SearchScopeV1(
        org_id=uuid4(), repository_id=uuid4(), snapshot_id=uuid4(), principal_id=uuid4()
    )


@pytest.mark.asyncio
async def test_missing_set_is_explicitly_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.retrieval_selection.require_retrieval_source", AsyncMock())
    session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = None
    session.execute.return_value = result
    assert await resolve_lexical_set(session, scope()) is None
    with pytest.raises(ScopeViolation, match="unavailable"):
        await resolve_lexical_set(session, scope(), chunk_set_id=uuid4())


@pytest.mark.asyncio
async def test_conflicting_set_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.retrieval_selection.require_retrieval_source", AsyncMock())
    session = AsyncMock()
    with pytest.raises(ScopeViolation, match="Conflicting"):
        await resolve_lexical_set(
            session, scope().model_copy(update={"chunk_set_id": uuid4()}), chunk_set_id=uuid4()
        )
    session.execute.assert_not_called()


def test_safe_content_integrity_boundary() -> None:
    text = "safe code"
    validate_safe_content(
        {"safe_content": text, "safe_content_sha256": hashlib.sha256(text.encode()).hexdigest()}
    )
    with pytest.raises(ScopeViolation, match="digest"):
        validate_safe_content(
            {
                "safe_content": "modified",
                "safe_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )


@pytest.mark.asyncio
async def test_hybrid_pins_every_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.contracts.indexing import IndexCoverageV1
    from app.contracts.search import (
        DenseSearchResponseV1,
        HybridSearchRequestV1,
        SearchMode,
        SearchResponseV1,
    )
    from app.services.hybrid_search import HybridSearchService
    from app.services.retrieval_selection import ResolvedChunkSetV1

    chosen = ResolvedChunkSetV1(
        uuid4(),
        uuid4(),
        "a" * 64,
        "b" * 64,
        IndexCoverageV1(admitted_files=0, selected_files=0, processed_files=0),
    )
    resolve = AsyncMock(return_value=chosen)
    monkeypatch.setattr("app.services.hybrid_search.resolve_lexical_set", resolve)
    monkeypatch.setattr("app.services.hybrid_search.require_retrieval_source", AsyncMock())
    lexical = MagicMock()
    dense = MagicMock()

    async def exact(request):
        assert request.scope.chunk_set_id == chosen.chunk_set_id
        return SearchResponseV1(
            scope=request.scope,
            mode=SearchMode.EXACT,
            query=request.query,
            total_candidates=0,
            hits=(),
            **chosen.response_fields(),
        )

    async def lexical_search(request):
        assert request.scope.chunk_set_id == chosen.chunk_set_id
        return SearchResponseV1(
            scope=request.scope,
            mode=SearchMode.LEXICAL,
            query=request.query,
            total_candidates=0,
            hits=(),
            **chosen.response_fields(),
        )

    async def dense_search(request):
        assert request.scope.chunk_set_id == chosen.chunk_set_id
        return DenseSearchResponseV1(
            scope=request.scope,
            query=request.query,
            index_id=None,
            embedding_model="none",
            availability="index_unavailable",
            coverage_status="unavailable",
            total_candidates=0,
            hits=(),
            **chosen.response_fields(),
        )

    lexical.search_exact = exact
    lexical.search_lexical = lexical_search
    dense.search_dense = dense_search
    result = await HybridSearchService(
        AsyncMock(), lexical_service=lexical, dense_service=dense
    ).search_hybrid(HybridSearchRequestV1(scope=scope(), query="test"))
    assert result.chunk_set_id == chosen.chunk_set_id
    assert result.lexical_availability == "available"
    assert result.dense_availability == "index_unavailable"
    resolve.assert_awaited_once()

    async def wrong_channel(request):
        return DenseSearchResponseV1(
            scope=request.scope,
            query=request.query,
            index_id=None,
            embedding_model="none",
            availability="index_unavailable",
            coverage_status="unavailable",
            total_candidates=0,
            hits=(),
        )

    dense.search_dense = wrong_channel
    with pytest.raises(ScopeViolation, match="different materializations"):
        await HybridSearchService(
            AsyncMock(), lexical_service=lexical, dense_service=dense
        ).search_hybrid(HybridSearchRequestV1(scope=scope(), query="test"))

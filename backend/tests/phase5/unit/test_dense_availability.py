from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.contracts.search import DenseSearchRequestV1, SearchScopeV1
from app.domain.errors import ScopeViolation
from app.services.dense_search import DenseSearchService


@pytest.fixture
def qualified_profile(monkeypatch):
    # These profile tests isolate unavailable-provider behavior, not publication authority.
    from app.contracts.indexing import IndexCoverageV1
    from app.services.retrieval_selection import ResolvedChunkSetV1

    selected = ResolvedChunkSetV1(
        uuid4(),
        uuid4(),
        "a" * 64,
        "b" * 64,
        IndexCoverageV1(admitted_files=0, selected_files=0, processed_files=0),
    )
    monkeypatch.setattr(
        "app.services.dense_search.resolve_lexical_set", AsyncMock(return_value=selected)
    )
    monkeypatch.setattr("app.services.dense_search.require_retrieval_source", AsyncMock())


def request():
    return DenseSearchRequestV1(
        scope=SearchScopeV1(
            org_id=uuid4(), repository_id=uuid4(), snapshot_id=uuid4(), principal_id=uuid4()
        ),
        query="query",
    )


@pytest.mark.asyncio
async def test_unconfigured_embedding_is_explicitly_unavailable(qualified_profile):
    service = DenseSearchService(AsyncMock())
    service._verify_scope = AsyncMock()
    service._resolve_index = AsyncMock(
        return_value={
            "id": uuid4(),
            "embedding_model": "model",
            "embedding_provider": "provider",
            "dimension": 2,
            "state": "ready",
        }
    )
    result = await service.search_dense(request())
    assert result.availability == "embedding_unconfigured"
    assert result.coverage_status == "unavailable"
    assert not result.hits


@pytest.mark.asyncio
async def test_authority_error_precedes_unavailable_fallback():
    service = DenseSearchService(AsyncMock())
    service._verify_scope = AsyncMock(side_effect=ScopeViolation("denied"))
    service._resolve_index = AsyncMock()
    with pytest.raises(ScopeViolation):
        await service.search_dense(request())
    service._resolve_index.assert_not_called()


@pytest.mark.asyncio
async def test_no_index_is_explicitly_unavailable(qualified_profile):
    service = DenseSearchService(AsyncMock())
    service._verify_scope = AsyncMock()
    service._resolve_index = AsyncMock(return_value=None)
    result = await service.search_dense(request())
    assert result.availability == "index_unavailable"
    assert result.index_id is None


@pytest.mark.parametrize(
    "vector", [(1.0,), (float("nan"), 1.0), (float("inf"), 0.0), (0.0, 0.0), (True, 1.0)]
)
def test_invalid_vectors_are_rejected(vector):
    with pytest.raises(ValueError, match="finite"):
        DenseSearchService._validate_vector(vector, 2)


@pytest.mark.asyncio
async def test_provider_profile_mismatch_is_not_fallback(qualified_profile):
    from app.contracts.provider_budget import EmbeddingResponseV1

    adapter = AsyncMock()
    adapter.embed.return_value = EmbeddingResponseV1(
        vector=(1.0, 0.0), token_count=1, model_id="wrong", provider="provider"
    )
    service = DenseSearchService(AsyncMock(), embedding_port=adapter)
    service._verify_scope = AsyncMock()
    service._resolve_index = AsyncMock(
        return_value={
            "id": uuid4(),
            "embedding_model": "model",
            "embedding_provider": "provider",
            "dimension": 2,
            "state": "ready",
        }
    )
    with pytest.raises(ValueError, match="profile"):
        await service.search_dense(request())
    adapter.embed.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_index_profile_never_dispatches(qualified_profile):
    adapter = AsyncMock()
    service = DenseSearchService(AsyncMock(), embedding_port=adapter)
    service._verify_scope = AsyncMock()
    service._resolve_index = AsyncMock(
        return_value={
            "id": uuid4(),
            "embedding_model": None,
            "embedding_provider": None,
            "dimension": None,
            "state": "ready",
        }
    )
    result = await service.search_dense(request())
    assert result.availability == "profile_unavailable"
    adapter.embed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["exact", "lexical", "dense"])
@pytest.mark.parametrize("snapshot_state", ["missing", "building", "foreign", "unavailable_source"])
@pytest.mark.parametrize("populated", [False, True])
async def test_all_channels_require_sealed_owned_retained_source(
    monkeypatch, channel, snapshot_state, populated
):
    from app.contracts.search import ExactSearchRequestV1, LexicalSearchRequestV1
    from app.domain.errors import DuplicateConflict
    from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
    from app.models.phase2_tables import PHASE2_TABLES
    from app.models.tables import memberships, repositories, source_snapshots
    from app.services.lexical_search import LexicalSearchService
    from sqlalchemy import Column, MetaData, Table
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    # SQLite models only authority queries, not PostgreSQL publication constraints/RLS.
    metadata = MetaData()
    tables = [
        memberships,
        PHASE2_TABLES["repository_grants"],
        repositories,
        source_snapshots,
        source_chunks,
        search_indices,
        chunk_embeddings,
    ]
    lightweight = {
        table.name: Table(
            table.name, metadata, *(Column(column.name, column.type) for column in table.columns)
        )
        for table in tables
    }
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    scope = request().scope
    assert scope is not None
    locator = AsyncMock(side_effect=DuplicateConflict("source unavailable"))
    monkeypatch.setattr("app.services.retrieval_authority.locate_source", locator)
    async with async_sessionmaker(engine, class_=AsyncSession)() as session:
        await session.execute(
            lightweight["memberships"]
            .insert()
            .values(org_id=scope.org_id, principal_id=scope.principal_id, role="member")
        )
        await session.execute(
            lightweight["repository_grants"]
            .insert()
            .values(
                org_id=scope.org_id,
                repository_id=scope.repository_id,
                principal_id=scope.principal_id,
                permission="read",
            )
        )
        await session.execute(
            lightweight["repositories"]
            .insert()
            .values(org_id=scope.org_id, id=scope.repository_id, source_kind="upload")
        )
        if snapshot_state != "missing":
            await session.execute(
                lightweight["source_snapshots"]
                .insert()
                .values(
                    org_id=scope.org_id,
                    id=scope.snapshot_id,
                    repository_id=uuid4() if snapshot_state == "foreign" else scope.repository_id,
                    state="building" if snapshot_state == "building" else "sealed",
                )
            )
        if populated:
            await session.execute(
                lightweight["source_chunks"]
                .insert()
                .values(
                    org_id=scope.org_id,
                    id=uuid4(),
                    repository_id=scope.repository_id,
                    snapshot_id=scope.snapshot_id,
                    safe_content="private source",
                    file_path="secret.py",
                )
            )
        adapter = AsyncMock()
        with pytest.raises(ScopeViolation):
            if channel == "dense":
                await DenseSearchService(session, embedding_port=adapter).search_dense(
                    DenseSearchRequestV1(scope=scope, query="private")
                )
            elif channel == "exact":
                await LexicalSearchService(session).search_exact(
                    ExactSearchRequestV1(scope=scope, query="private")
                )
            else:
                await LexicalSearchService(session).search_lexical(
                    LexicalSearchRequestV1(scope=scope, query="private")
                )
        adapter.embed.assert_not_called()
        if snapshot_state == "unavailable_source":
            locator.assert_awaited_once()
        else:
            locator.assert_not_called()
    await engine.dispose()

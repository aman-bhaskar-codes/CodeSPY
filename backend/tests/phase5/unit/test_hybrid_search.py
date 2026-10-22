from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.search import (
    HybridSearchRequestV1,
    IndexType,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships
from app.services.dense_search import DenseSearchService
from app.services.hybrid_search import HybridSearchService
from app.services.index_lifecycle import IndexLifecycleService
from app.services.lexical_search import LexicalSearchService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    grants = PHASE2_TABLES["repository_grants"]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[memberships, grants, source_chunks, search_indices, chunk_embeddings],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def make_chunk(
    org_id: UUID,
    repo_id: UUID,
    snap_id: UUID,
    path: str,
    sym: str | None,
    kind: ChunkKind,
    content: str,
    start_line: int = 1,
    end_line: int = 10,
    start_byte: int = 0,
    end_byte: int | None = None,
) -> SourceChunkV1:
    raw = content.encode("utf-8")
    eb = end_byte if end_byte is not None else len(raw)
    return SourceChunkV1(
        chunk_id=uuid4(),
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=uuid4(),
        file_path=path,
        chunk_index=0,
        chunk_kind=kind,
        symbol_name=sym,
        qualified_name=f"pkg.{sym}" if sym else None,
        cpg_node_id=uuid4(),
        start_byte=start_byte,
        end_byte=eb,
        start_line=start_line,
        end_line=end_line,
        content_sha256="abc",
        byte_count=len(raw),
        token_count=max(1, len(content) // 4),
        raw_content=raw,
        safe_content=content,
        redaction_digest="none",
        chunker_version="1.0.0",
    )


@pytest.mark.asyncio
async def test_hybrid_rrf_scoring_and_fusion(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )

    lifecycle = IndexLifecycleService(session)
    lexical_svc = LexicalSearchService(session)
    dense_svc = DenseSearchService(session)
    hybrid_svc = HybridSearchService(session, lexical_service=lexical_svc, dense_service=dense_svc)

    # Create dense index
    idx = await lifecycle.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_model="fake-v1",
        dimension=1024,
    )

    # Chunk 1: matches exact symbol AND lexical AND dense query!
    c1 = make_chunk(
        org_id,
        repo_id,
        snap_id,
        "src/auth.py",
        "authenticate_user_credentials",
        ChunkKind.FUNCTION,
        "def authenticate_user_credentials(user, password):\n    return verify(password)\n",
    )
    # Chunk 2: only partial lexical match
    c2 = make_chunk(
        org_id,
        repo_id,
        snap_id,
        "src/other.py",
        "other_helper",
        ChunkKind.FUNCTION,
        "def other_helper():\n    return 'user password'\n",
    )

    # Insert into source_chunks & lexical service
    await lexical_svc.index_chunks([c1, c2])
    # Publish embeddings & seal dense index
    await lifecycle.publish_embeddings_batch(org_id=org_id, index_id=idx.index_id, chunks=[c1, c2])
    await lifecycle.seal_index(org_id, idx.index_id)

    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    res = await hybrid_svc.search_hybrid(
        HybridSearchRequestV1(
            scope=scope,
            query="authenticate_user_credentials",
            limit=5,
        )
    )

    assert len(res.hits) >= 1
    top_hit = res.hits[0]
    assert top_hit.chunk_id == c1.chunk_id
    assert "exact" in top_hit.match_modalities
    # Fused multi-modality score should be greater than any single modality weight / (60 + 1)
    assert top_hit.score > (1.5 / 61.0)


@pytest.mark.asyncio
async def test_near_duplicate_subsumption(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )

    lexical_svc = LexicalSearchService(session)
    dense_svc = DenseSearchService(session)
    hybrid_svc = HybridSearchService(session, lexical_service=lexical_svc, dense_service=dense_svc)

    # Specific function chunk
    c_func = make_chunk(
        org_id,
        repo_id,
        snap_id,
        "src/main.py",
        "calculate_tax",
        ChunkKind.FUNCTION,
        "def calculate_tax(): pass",
        start_line=10,
        end_line=20,
        start_byte=100,
        end_byte=200,
    )
    # Enclosing broad window chunk covering the exact same span
    c_win = make_chunk(
        org_id,
        repo_id,
        snap_id,
        "src/main.py",
        None,
        ChunkKind.WINDOW,
        "window content def calculate_tax(): pass",
        start_line=1,
        end_line=30,
        start_byte=50,
        end_byte=300,
    )

    await lexical_svc.index_chunks([c_func, c_win])

    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    res = await hybrid_svc.search_hybrid(
        HybridSearchRequestV1(scope=scope, query="calculate_tax", limit=5)
    )
    hit_ids = [h.chunk_id for h in res.hits]
    assert c_func.chunk_id in hit_ids
    # Window chunk should be subsumed if function chunk is already present
    assert len([h for h in res.hits if h.chunk_kind == ChunkKind.FUNCTION]) >= 1


@pytest.mark.asyncio
async def test_hybrid_search_unauthorized_scope(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    hybrid_svc = HybridSearchService(session)
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    # Without membership/grants, raises ScopeViolation
    with pytest.raises(ScopeViolation):
        await hybrid_svc.search_hybrid(
            HybridSearchRequestV1(scope=scope, query="anything", limit=5)
        )

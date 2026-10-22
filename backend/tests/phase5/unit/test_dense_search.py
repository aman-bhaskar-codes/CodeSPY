import json
import math
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.adapters.embedding.titan import AmazonTitanEmbeddingAdapter
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.provider_budget import EmbeddingRequestV1
from app.contracts.search import (
    DenseSearchRequestV1,
    IndexType,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships
from app.services.dense_search import DenseSearchService
from app.services.index_lifecycle import IndexLifecycleService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATASET_PATH = Path(__file__).resolve().parent.parent / "eval" / "relevance_dataset.json"
RELEVANCE_DATASET = json.loads(DATASET_PATH.read_text())


@pytest.mark.asyncio
async def test_titan_v2_offline_simulation() -> None:
    adapter = AmazonTitanEmbeddingAdapter(simulate_offline=True)

    # Test 1024 dimensions
    res1024 = await adapter.embed(
        EmbeddingRequestV1(text="Hello world", model_id=adapter.MODEL_ID, dimensions=1024)
    )
    assert len(res1024.vector) == 1024
    norm1024 = math.sqrt(sum(x * x for x in res1024.vector))
    assert math.isclose(norm1024, 1.0, rel_tol=1e-5)

    # Test 512 dimensions
    res512 = await adapter.embed(
        EmbeddingRequestV1(text="Hello world", model_id=adapter.MODEL_ID, dimensions=512)
    )
    assert len(res512.vector) == 512

    # Test unsupported dimensions
    with pytest.raises(ValueError, match="Unsupported dimension"):
        await adapter.embed(
            EmbeddingRequestV1(text="Fail", model_id=adapter.MODEL_ID, dimensions=128)
        )


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
    content: str,
) -> SourceChunkV1:
    raw = content.encode("utf-8")
    return SourceChunkV1(
        chunk_id=uuid4(),
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=uuid4(),
        file_path=path,
        chunk_index=0,
        chunk_kind=ChunkKind.FUNCTION,
        symbol_name=sym,
        qualified_name=f"pkg.{sym}" if sym else None,
        cpg_node_id=uuid4(),
        start_byte=0,
        end_byte=len(raw),
        start_line=1,
        end_line=content.count("\n") + 1,
        content_sha256="abc",
        byte_count=len(raw),
        token_count=max(1, len(content) // 4),
        raw_content=raw,
        safe_content=content,
        redaction_digest="none",
        chunker_version="1.0.0",
    )


@pytest.mark.asyncio
async def test_dense_search_exact_match(session: AsyncSession) -> None:
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
    idx = await lifecycle.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_model="fake-v1",
        dimension=1024,
    )

    chunk = make_chunk(
        org_id,
        repo_id,
        snap_id,
        "src/auth.py",
        "authenticate_user_credentials",
        "def authenticate_user_credentials(user, password):\n    return verify(password)\n",
    )
    # Insert source chunk row first for foreign key join
    await session.execute(
        source_chunks.insert().values(
            org_id=org_id,
            id=chunk.chunk_id,
            repository_id=repo_id,
            snapshot_id=snap_id,
            file_id=chunk.file_id,
            file_path=chunk.file_path,
            chunk_index=0,
            chunk_kind="function",
            symbol_name=chunk.symbol_name,
            qualified_name=chunk.qualified_name,
            cpg_node_id=chunk.cpg_node_id,
            start_byte=0,
            end_byte=chunk.end_byte,
            start_line=1,
            end_line=chunk.end_line,
            content_sha256="abc",
            byte_count=chunk.byte_count,
            estimated_tokens=chunk.token_count,
            safe_content=chunk.safe_content,
            redaction_digest="none",
            chunker_version="1.0.0",
        )
    )
    await lifecycle.publish_embeddings_batch(
        org_id=org_id,
        index_id=idx.index_id,
        chunks=[chunk],
    )
    await lifecycle.seal_index(org_id, idx.index_id)

    search_svc = DenseSearchService(session)
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    resp = await search_svc.search_dense(
        DenseSearchRequestV1(
            scope=scope,
            query=(
                "def authenticate_user_credentials(user, password):\n    return verify(password)\n"
            ),
            limit=5,
        )
    )
    assert len(resp.hits) == 1
    hit = resp.hits[0]
    assert hit.file_path == "src/auth.py"
    assert hit.symbol_name == "authenticate_user_credentials"
    # Identical text in FakeEmbeddingAdapter produces exact identical vector
    assert hit.score > 0.99


@pytest.mark.asyncio
async def test_dense_search_multitenant_isolation(session: AsyncSession) -> None:
    org_a = uuid4()
    org_b = uuid4()
    principal_a = uuid4()
    repo_a = uuid4()
    repo_b = uuid4()
    snap_a = uuid4()
    snap_b = uuid4()

    # Org A access
    await session.execute(
        memberships.insert().values(org_id=org_a, principal_id=principal_a, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_a,
            repository_id=repo_a,
            principal_id=principal_a,
            permission="read",
            revoked_at=None,
        )
    )

    lifecycle = IndexLifecycleService(session)

    # Index in Repo A
    idx_a = await lifecycle.create_index(
        org_id=org_a,
        repository_id=repo_a,
        snapshot_id=snap_a,
        index_type=IndexType.DENSE,
    )
    chunk_a = make_chunk(org_a, repo_a, snap_a, "src/auth.py", "auth", "shared code")
    await session.execute(
        source_chunks.insert().values(
            org_id=org_a,
            id=chunk_a.chunk_id,
            repository_id=repo_a,
            snapshot_id=snap_a,
            file_id=chunk_a.file_id,
            file_path=chunk_a.file_path,
            chunk_index=0,
            chunk_kind="function",
            symbol_name="auth",
            qualified_name="pkg.auth",
            cpg_node_id=chunk_a.cpg_node_id,
            start_byte=0,
            end_byte=chunk_a.end_byte,
            start_line=1,
            end_line=chunk_a.end_line,
            content_sha256="abc",
            byte_count=chunk_a.byte_count,
            estimated_tokens=chunk_a.token_count,
            safe_content=chunk_a.safe_content,
            redaction_digest="none",
            chunker_version="1.0.0",
        )
    )
    await lifecycle.publish_embeddings_batch(
        org_id=org_a,
        index_id=idx_a.index_id,
        chunks=[chunk_a],
    )
    await lifecycle.seal_index(org_a, idx_a.index_id)

    # Index in Repo B (identical text)
    idx_b = await lifecycle.create_index(
        org_id=org_b,
        repository_id=repo_b,
        snapshot_id=snap_b,
        index_type=IndexType.DENSE,
    )
    chunk_b = make_chunk(org_b, repo_b, snap_b, "src/auth.py", "auth", "shared code")
    await session.execute(
        source_chunks.insert().values(
            org_id=org_b,
            id=chunk_b.chunk_id,
            repository_id=repo_b,
            snapshot_id=snap_b,
            file_id=chunk_b.file_id,
            file_path=chunk_b.file_path,
            chunk_index=0,
            chunk_kind="function",
            symbol_name="auth",
            qualified_name="pkg.auth",
            cpg_node_id=chunk_b.cpg_node_id,
            start_byte=0,
            end_byte=chunk_b.end_byte,
            start_line=1,
            end_line=chunk_b.end_line,
            content_sha256="abc",
            byte_count=chunk_b.byte_count,
            estimated_tokens=chunk_b.token_count,
            safe_content=chunk_b.safe_content,
            redaction_digest="none",
            chunker_version="1.0.0",
        )
    )
    await lifecycle.publish_embeddings_batch(
        org_id=org_b,
        index_id=idx_b.index_id,
        chunks=[chunk_b],
    )
    await lifecycle.seal_index(org_b, idx_b.index_id)

    search_svc = DenseSearchService(session)

    # Query within Repo A scope
    scope_a = SearchScopeV1(
        org_id=org_a,
        repository_id=repo_a,
        snapshot_id=snap_a,
        principal_id=principal_a,
    )
    resp_a = await search_svc.search_dense(
        DenseSearchRequestV1(scope=scope_a, query="shared code", limit=10)
    )
    assert len(resp_a.hits) == 1
    assert resp_a.hits[0].chunk_id == chunk_a.chunk_id

    # Query within Repo B scope by principal A (unauthorized -> ScopeViolation)
    scope_b = SearchScopeV1(
        org_id=org_b,
        repository_id=repo_b,
        snapshot_id=snap_b,
        principal_id=principal_a,
    )
    with pytest.raises(ScopeViolation):
        await search_svc.search_dense(
            DenseSearchRequestV1(scope=scope_b, query="shared code", limit=10)
        )

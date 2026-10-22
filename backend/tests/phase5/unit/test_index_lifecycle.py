from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.provider_budget import PriceScheduleV1
from app.contracts.search import IndexState, IndexType
from app.contracts.source_view import PrivacyPolicyV1
from app.domain.errors import ScopeViolation
from app.models.budget_tables import (
    embedding_budgets,
    embedding_reservations,
    price_schedules,
)
from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships
from app.services.index_lifecycle import IndexLifecycleService
from app.services.provider_budget import ProviderBudgetService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    grants = PHASE2_TABLES["repository_grants"]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    memberships,
                    grants,
                    source_chunks,
                    search_indices,
                    chunk_embeddings,
                    embedding_budgets,
                    embedding_reservations,
                    price_schedules,
                ],
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
    idx: int,
    content: str,
) -> SourceChunkV1:
    raw = content.encode("utf-8")
    return SourceChunkV1(
        chunk_id=uuid4(),
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=uuid4(),
        file_path="src/main.py",
        chunk_index=idx,
        chunk_kind=ChunkKind.FUNCTION,
        symbol_name="handle_request",
        qualified_name="src.main.handle_request",
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
async def test_create_and_seal_index(session: AsyncSession) -> None:
    svc = IndexLifecycleService(session)
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    idx = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_provider="fake",
        embedding_model="fake-v1",
        dimension=1024,
        total_chunks=2,
    )

    assert idx.state == IndexState.BUILDING
    assert idx.total_chunks == 2
    assert idx.indexed_chunks == 0

    chunk1 = make_chunk(org_id, repo_id, snap_id, 0, "def foo(): return 1\n")
    chunk2 = make_chunk(org_id, repo_id, snap_id, 1, "def bar(): return 2\n")

    res = await svc.publish_embeddings_batch(
        org_id=org_id,
        index_id=idx.index_id,
        chunks=[chunk1, chunk2],
    )
    assert res["indexed"] == 2
    assert res["failed"] == 0

    # Seal the index
    sealed = await svc.seal_index(org_id, idx.index_id)
    assert sealed.state == IndexState.READY
    assert sealed.sealed_at is not None

    # Verify active index retrieval
    active = await svc.get_active_index(org_id, repo_id, snap_id, IndexType.DENSE)
    assert active is not None
    assert active.index_id == idx.index_id


@pytest.mark.asyncio
async def test_seal_index_partial_ready_and_failed(session: AsyncSession) -> None:
    svc = IndexLifecycleService(session)
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    # Index with 100 chunks, 3 failures (3% failure rate <= 5% tolerance -> PARTIAL_READY)
    idx_partial = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        total_chunks=100,
    )
    from sqlalchemy import update

    await session.execute(
        update(search_indices)
        .where(search_indices.c.id == idx_partial.index_id)
        .values(indexed_chunks=97, failed_chunks=3)
    )

    sealed_partial = await svc.seal_index(org_id, idx_partial.index_id, tolerance_ratio=0.05)
    assert sealed_partial.state == IndexState.PARTIAL_READY

    # Index with 100 chunks, 10 failures (10% failure rate > 5% tolerance -> FAILED)
    idx_failed = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        total_chunks=100,
    )
    await session.execute(
        update(search_indices)
        .where(search_indices.c.id == idx_failed.index_id)
        .values(indexed_chunks=90, failed_chunks=10)
    )

    sealed_failed = await svc.seal_index(org_id, idx_failed.index_id, tolerance_ratio=0.05)
    assert sealed_failed.state == IndexState.FAILED


@pytest.mark.asyncio
async def test_sealing_supersedes_older_ready_indices(session: AsyncSession) -> None:
    svc = IndexLifecycleService(session)
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    idx1 = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
    )
    sealed1 = await svc.seal_index(org_id, idx1.index_id)
    assert sealed1.state == IndexState.READY

    idx2 = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
    )
    sealed2 = await svc.seal_index(org_id, idx2.index_id)
    assert sealed2.state == IndexState.READY

    # idx1 should now be STALE
    old = await svc.get_index(org_id, idx1.index_id)
    assert old is not None
    assert old.state == IndexState.STALE


@pytest.mark.asyncio
async def test_publish_with_lease_fencing(session: AsyncSession) -> None:
    svc = IndexLifecycleService(session)
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    idx = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.LEXICAL,
    )

    chunk = make_chunk(org_id, repo_id, snap_id, 0, "print('hello')\n")

    now = datetime.now(UTC)
    bad_lease = AttemptLeaseV2(
        org_id=uuid4(),
        job_id=uuid4(),
        attempt_id=uuid4(),
        fence=1,
        owner="worker_test",
        lease_expires_at=now + timedelta(minutes=5),
        heartbeat_at=now,
    )
    with pytest.raises(ScopeViolation):
        await svc.publish_embeddings_batch(
            org_id=org_id,
            index_id=idx.index_id,
            chunks=[chunk],
            lease=bad_lease,
        )


@pytest.mark.asyncio
async def test_cloud_provider_privacy_policy_rejection(session: AsyncSession) -> None:
    svc = IndexLifecycleService(session)
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    idx = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_provider="aws_bedrock",
    )

    chunk = make_chunk(org_id, repo_id, snap_id, 0, "print('secret')\n")
    strict_policy = PrivacyPolicyV1(allow_cloud_providers=False)

    with pytest.raises(ScopeViolation, match="prohibits cloud embedding provider"):
        await svc.publish_embeddings_batch(
            org_id=org_id,
            index_id=idx.index_id,
            chunks=[chunk],
            privacy_policy=strict_policy,
        )


@pytest.mark.asyncio
async def test_budget_integration_during_indexing(session: AsyncSession) -> None:
    budget_svc = ProviderBudgetService(session)
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    now = datetime.now(UTC)
    await budget_svc.set_price_schedule(
        PriceScheduleV1(
            provider="aws_bedrock",
            model_id="amazon.titan-embed-text-v2:0",
            version="2026-10-22",
            input_micros_per_1k=20,  # $0.00002 / 1k tokens
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=365),
        )
    )
    await budget_svc.ensure_budget(
        org_id=org_id,
        repository_id=repo_id,
        initial_balance_micros=100_000,
    )

    svc = IndexLifecycleService(session, budget_service=budget_svc)
    idx = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_provider="aws_bedrock",
        embedding_model="amazon.titan-embed-text-v2:0",
        dimension=1024,
    )

    chunk = make_chunk(org_id, repo_id, snap_id, 0, "x = 10\ny = 20\n")
    allow_policy = PrivacyPolicyV1(allow_cloud_providers=True)

    res = await svc.publish_embeddings_batch(
        org_id=org_id,
        index_id=idx.index_id,
        chunks=[chunk],
        privacy_policy=allow_policy,
    )
    assert res["indexed"] == 1

    # Check budget ledger balance decreased
    b_info = await budget_svc.get_budget(org_id, repo_id)
    assert b_info["reserved_micros"] == 0
    assert int(b_info["balance_micros"]) < 100_000

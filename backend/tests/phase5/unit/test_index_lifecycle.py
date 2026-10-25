from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.adapters.embedding.fake import FakeEmbeddingAdapter
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.jobs import AttemptLeaseV2
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
        from app.models.index_tables import index_publication_receipts, retrieval_chunk_sets
        from sqlalchemy import Column, MetaData, Table

        lightweight = MetaData()
        for table in (retrieval_chunk_sets, index_publication_receipts):
            Table(
                table.name,
                lightweight,
                *(
                    Column(c.name, c.type)
                    for c in table.columns
                    if c.name
                    in {
                        "org_id",
                        "id",
                        "repository_id",
                        "snapshot_id",
                        "chunk_set_id",
                        "materialization_hash",
                    }
                ),
            )
        await conn.run_sync(lightweight.create_all)
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
    svc = IndexLifecycleService(session, embedding_port=FakeEmbeddingAdapter())
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

    # Legacy unassociated rows are inspectable but never selected as qualified indexes.
    active = await svc.get_active_index(org_id, repo_id, snap_id, IndexType.DENSE)
    assert active is None
    assert await svc.get_index(org_id, idx.index_id) is not None


@pytest.mark.asyncio
async def test_seal_index_partial_ready_and_failed(session: AsyncSession) -> None:
    svc = IndexLifecycleService(session, embedding_port=FakeEmbeddingAdapter())
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
    svc = IndexLifecycleService(session, embedding_port=FakeEmbeddingAdapter())
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
    svc = IndexLifecycleService(session, embedding_port=FakeEmbeddingAdapter())
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
    svc = IndexLifecycleService(session, embedding_port=FakeEmbeddingAdapter())
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    idx = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_provider="aws_bedrock",
        embedding_model="synthetic-model",
        dimension=2,
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
async def test_external_budget_remains_untouched_without_worker_binding(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    budget_svc = ProviderBudgetService(session)
    org_id, repo_id, snap_id, principal = (uuid4() for _ in range(4))
    await budget_svc.ensure_budget(
        org_id=org_id,
        repository_id=repo_id,
        initial_balance_micros=100_000,
    )
    authority = AsyncMock()
    monkeypatch.setattr("app.services.index_lifecycle.require_repository", authority, raising=False)
    adapter = FakeEmbeddingAdapter()
    svc = IndexLifecycleService(session, embedding_port=adapter, budget_service=budget_svc)
    idx = await svc.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_provider="external",
        embedding_model="synthetic-model",
        dimension=2,
    )
    with pytest.raises(RuntimeError, match="worker binding"):
        await svc.publish_embeddings_batch(
            org_id=org_id,
            index_id=idx.index_id,
            chunks=[make_chunk(org_id, repo_id, snap_id, 0, "private source")],
            privacy_policy=PrivacyPolicyV1(allow_cloud_providers=True),
            principal_id=principal,
        )
    assert not adapter.calls
    b_info = await budget_svc.get_budget(org_id, repo_id)
    assert b_info["reserved_micros"] == 0
    assert int(b_info["balance_micros"]) == 100_000


@pytest.mark.asyncio
async def test_dense_publication_requires_explicit_adapter(session: AsyncSession) -> None:
    service = IndexLifecycleService(session)
    org_id, repo_id, snap_id = (uuid4() for _ in range(3))
    idx = await service.create_index(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        index_type=IndexType.DENSE,
        embedding_provider="fake",
        embedding_model="fake-v1",
        dimension=1024,
    )
    chunk = make_chunk(org_id, repo_id, snap_id, 0, "x = 1\n")
    with pytest.raises(RuntimeError, match="not configured"):
        await service.publish_embeddings_batch(org_id=org_id, index_id=idx.index_id, chunks=[chunk])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [IndexType.DENSE, IndexType.LEXICAL])
@pytest.mark.parametrize("field", ["org_id", "repository_id", "snapshot_id"])
async def test_chunk_scope_mismatch_rejected_before_dispatch_or_progress(session, kind, field):
    adapter = FakeEmbeddingAdapter()
    service = IndexLifecycleService(session, embedding_port=adapter)
    org, repo, snap = (uuid4() for _ in range(3))
    index = await service.create_index(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        index_type=kind,
        embedding_provider="fake",
        embedding_model="fake-v1",
        dimension=2,
    )
    good = make_chunk(org, repo, snap, 0, "private source")
    bad = good.model_copy(update={field: uuid4()})
    with pytest.raises(ScopeViolation, match="scope"):
        await service.publish_embeddings_batch(
            org_id=org, index_id=index.index_id, chunks=[good, bad]
        )
    assert adapter.calls == []
    record = await service.get_index(org, index.index_id)
    assert record.indexed_chunks == 0 and record.failed_chunks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["embedding_provider", "embedding_model", "dimension"])
async def test_dense_missing_profile_is_not_defaulted(session, field):
    adapter = FakeEmbeddingAdapter()
    service = IndexLifecycleService(session, embedding_port=adapter)
    org, repo, snap = (uuid4() for _ in range(3))
    profile = {"embedding_provider": "fake", "embedding_model": "fake-v1", "dimension": 2}
    profile[field] = None
    index = await service.create_index(
        org_id=org, repository_id=repo, snapshot_id=snap, index_type=IndexType.DENSE, **profile
    )
    with pytest.raises(ValueError, match="profile"):
        await service.publish_embeddings_batch(
            org_id=org,
            index_id=index.index_id,
            chunks=[make_chunk(org, repo, snap, 0, "private source")],
        )
    assert not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["policy", "budget", "principal"])
async def test_external_dispatch_requires_explicit_policy_budget_and_principal(session, missing):
    from unittest.mock import AsyncMock

    adapter, budget = FakeEmbeddingAdapter(), AsyncMock()
    service = IndexLifecycleService(
        session, embedding_port=adapter, budget_service=None if missing == "budget" else budget
    )
    org, repo, snap = (uuid4() for _ in range(3))
    index = await service.create_index(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        index_type=IndexType.DENSE,
        embedding_provider="external",
        embedding_model="qualified-test-model",
        dimension=2,
    )
    with pytest.raises(ScopeViolation):
        await service.publish_embeddings_batch(
            org_id=org,
            index_id=index.index_id,
            chunks=[make_chunk(org, repo, snap, 0, "private source")],
            principal_id=None if missing == "principal" else uuid4(),
            privacy_policy=None
            if missing == "policy"
            else PrivacyPolicyV1(allow_cloud_providers=True),
        )
    assert not adapter.calls
    budget.reserve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["provider", "model", "dimension", "nan", "inf", "zero"])
async def test_invalid_embedding_response_is_never_persisted(session, bad):
    from unittest.mock import AsyncMock

    from app.contracts.provider_budget import EmbeddingResponseV1
    from sqlalchemy import func, select

    vector = {
        "dimension": (1.0,),
        "nan": (float("nan"), 0.0),
        "inf": (float("inf"), 0.0),
        "zero": (0.0, 0.0),
    }.get(bad, (1.0, 0.0))
    adapter = AsyncMock()
    adapter.embed.return_value = EmbeddingResponseV1(
        vector=vector,
        token_count=1,
        provider="wrong" if bad == "provider" else "fake",
        model_id="wrong" if bad == "model" else "fake-v1",
    )
    service = IndexLifecycleService(session, embedding_port=adapter)
    org, repo, snap = (uuid4() for _ in range(3))
    index = await service.create_index(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        index_type=IndexType.DENSE,
        embedding_provider="fake",
        embedding_model="fake-v1",
        dimension=2,
    )
    result = await service.publish_embeddings_batch(
        org_id=org,
        index_id=index.index_id,
        chunks=[make_chunk(org, repo, snap, 0, "private source")],
    )
    assert result == {"indexed": 0, "failed": 1}
    assert await session.scalar(select(func.count()).select_from(chunk_embeddings)) == 0


@pytest.mark.asyncio
async def test_external_principal_must_have_current_write_authority(session, monkeypatch):
    from unittest.mock import AsyncMock

    adapter, budget = FakeEmbeddingAdapter(), AsyncMock()
    denied = AsyncMock(side_effect=ScopeViolation("repository unavailable"))
    monkeypatch.setattr("app.services.index_lifecycle.require_repository", denied, raising=False)
    service = IndexLifecycleService(session, embedding_port=adapter, budget_service=budget)
    org, repo, snap, principal = (uuid4() for _ in range(4))
    index = await service.create_index(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        index_type=IndexType.DENSE,
        embedding_provider="external",
        embedding_model="qualified-test-model",
        dimension=2,
    )
    with pytest.raises(ScopeViolation):
        await service.publish_embeddings_batch(
            org_id=org,
            index_id=index.index_id,
            chunks=[make_chunk(org, repo, snap, 0, "private source")],
            principal_id=principal,
            privacy_policy=PrivacyPolicyV1(allow_cloud_providers=True),
        )
    assert not adapter.calls
    budget.reserve.assert_not_called()
    denied.assert_awaited_once_with(session, org, principal, repo, write=True)


@pytest.mark.asyncio
async def test_external_publication_is_unavailable_without_worker_binding(session, monkeypatch):
    from unittest.mock import AsyncMock

    adapter, budget = FakeEmbeddingAdapter(), AsyncMock()
    authority = AsyncMock()
    monkeypatch.setattr("app.services.index_lifecycle.require_repository", authority, raising=False)
    service = IndexLifecycleService(session, embedding_port=adapter, budget_service=budget)
    org, repo, snap, principal = (uuid4() for _ in range(4))
    index = await service.create_index(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        index_type=IndexType.DENSE,
        embedding_provider="external",
        embedding_model="qualified-test-model",
        dimension=2,
    )
    with pytest.raises(RuntimeError, match="worker binding"):
        await service.publish_embeddings_batch(
            org_id=org,
            index_id=index.index_id,
            chunks=[make_chunk(org, repo, snap, 0, "private source")],
            principal_id=principal,
            privacy_policy=PrivacyPolicyV1(allow_cloud_providers=True),
        )
    assert not adapter.calls
    budget.reserve.assert_not_called()
    authority.assert_awaited_once_with(session, org, principal, repo, write=True)


@pytest.mark.asyncio
async def test_lease_without_persisted_index_binding_is_unavailable(session):
    adapter = FakeEmbeddingAdapter()
    service = IndexLifecycleService(session, embedding_port=adapter)
    org, repo, snap = (uuid4() for _ in range(3))
    index = await service.create_index(
        org_id=org, repository_id=repo, snapshot_id=snap, index_type=IndexType.LEXICAL
    )
    now = datetime.now(UTC)
    lease = AttemptLeaseV2(
        org_id=org,
        job_id=uuid4(),
        attempt_id=uuid4(),
        fence=1,
        owner="synthetic-worker",
        lease_expires_at=now + timedelta(minutes=5),
        heartbeat_at=now,
    )
    with pytest.raises(RuntimeError, match="worker binding"):
        await service.publish_embeddings_batch(
            org_id=org,
            index_id=index.index_id,
            chunks=[make_chunk(org, repo, snap, 0, "private source")],
            lease=lease,
        )
    assert not adapter.calls
    record = await service.get_index(org, index.index_id)
    assert record.indexed_chunks == 0 and record.failed_chunks == 0


@pytest.mark.asyncio
async def test_legacy_lifecycle_preserves_qualified_indexes(session: AsyncSession) -> None:
    from sqlalchemy import select, update

    svc = IndexLifecycleService(session)
    org_id, repository_id, snapshot_id = uuid4(), uuid4(), uuid4()
    qualified = await svc.create_index(
        org_id=org_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        index_type=IndexType.LEXICAL,
    )
    await session.execute(
        update(search_indices)
        .where(search_indices.c.id == qualified.index_id)
        .values(chunk_set_id=uuid4(), state="ready")
    )
    with pytest.raises(ScopeViolation, match="immutable"):
        await svc.seal_index(org_id, qualified.index_id)
    other = await svc.create_index(
        org_id=org_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        index_type=IndexType.LEXICAL,
    )
    await svc.seal_index(org_id, other.index_id)
    assert (
        await session.scalar(
            select(search_indices.c.state).where(search_indices.c.id == qualified.index_id)
        )
        == "ready"
    )
    # A non-null association alone cannot qualify an index without its durable parent/receipt.
    assert await svc.get_active_index(org_id, repository_id, snapshot_id) is None

"""The embed operation end to end: accounted calls, durable staging, resume and publication."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.provider_budget import EmbeddingRequestV1, EmbeddingResponseV1
from app.domain.errors import StaleAttempt
from app.services.embed_job import EmbedFailure, EmbedOperation
from sqlalchemy import text

from tests.retrieval.storage.test_dense_guards import DIM, MODEL, PROVIDER, embed_case

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0046)",
)


class Embedder:
    """Deterministic synthetic provider; counts calls and can fail on demand."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[str] = []
        self.fail = fail

    async def embed(self, request: EmbeddingRequestV1) -> EmbeddingResponseV1:
        self.calls.append(request.text)
        if self.fail:
            raise self.fail
        seed = (sum(request.text.encode()) % 97) + 1
        vector = tuple((seed * (i + 1)) / 10_000 for i in range(request.dimensions))
        return EmbeddingResponseV1(
            vector=vector,
            token_count=10,
            model_id=MODEL,
            provider=PROVIDER,
            provider_request_id="req",
        )


def lease_for(case, fence=None):
    job_id, attempt_id, current = case["binding"]
    now = datetime.now(UTC)
    return AttemptLeaseV2(
        org_id=case["ctx"].org_id,
        job_id=job_id,
        attempt_id=attempt_id,
        fence=fence or current,
        owner="embed-test",
        lease_expires_at=now + timedelta(seconds=60),
        heartbeat_at=now,
    )


async def scalar(migration_factory, query, **params):
    async with migration_factory() as session:
        return await session.scalar(text(query), params)


async def test_operation_embeds_every_chunk_once_and_publishes_a_sealed_dense_index(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    embedder = Embedder()
    receipt = await EmbedOperation(broker_factory, embedder).execute(lease_for(case))
    count = await scalar(
        migration_factory,
        "SELECT count(*) FROM source_chunks WHERE chunk_set_id=:s",
        s=case["set_id"],
    )
    assert len(embedder.calls) == count == receipt.chunk_count
    assert receipt.index_id == case["index_id"] and receipt.coverage == "complete"
    assert receipt.token_total == 10 * count
    assert (
        await scalar(
            migration_factory,
            "SELECT state FROM search_indices WHERE id=:i",
            i=case["index_id"],
        )
        == "ready"
    )
    assert await scalar(migration_factory, "SELECT count(*) FROM embed_vector_stage") == 0
    assert await scalar(migration_factory, "SELECT count(*) FROM embed_spend_ledger") == count
    assert (
        await scalar(
            migration_factory,
            "SELECT count(*) FROM embedding_reservations WHERE state='committed' "
            "AND settled_micros=10",
        )
        == count
    )
    assert (
        await scalar(migration_factory, "SELECT state FROM jobs WHERE id=:j", j=case["binding"][0])
        == "succeeded"
    )
    # the published vectors are searchable by pgvector cosine distance
    nearest = await scalar(
        migration_factory,
        "SELECT count(*) FROM (SELECT chunk_id FROM chunk_embeddings WHERE index_id=:i "
        "ORDER BY embedding <=> (SELECT embedding FROM chunk_embeddings WHERE index_id=:i LIMIT 1) "
        "LIMIT 3) q",
        i=case["index_id"],
    )
    assert nearest >= 1
    # a repeat of the finished job publishes nothing new and calls no provider
    again = await EmbedOperation(broker_factory, embedder).finalizer.finish(
        lease_for(case), receipt_spec(case), await spec_id(migration_factory, case)
    )
    assert again.index_id == receipt.index_id and len(embedder.calls) == count


def receipt_spec(case):
    from app.contracts.embedding_index import EmbedConfigV1, EmbedSpecV1

    return EmbedSpecV1(
        org_id=case["ctx"].org_id,
        repository_id=case["spec"].repository_id,
        snapshot_id=case["spec"].snapshot_id,
        principal_id=case["ctx"].principal_id,
        chunk_set_id=case["set_id"],
        config=EmbedConfigV1(
            provider=PROVIDER, model_id=MODEL, dimension=DIM, normalize=True, price_version="test"
        ),
    )


async def spec_id(migration_factory, case):
    return case["spec_id"]


async def test_crash_after_staging_resumes_without_paying_or_calling_the_provider_again(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    embedder = Embedder()
    operation = EmbedOperation(broker_factory, embedder, concurrency=1)
    real = EmbedOperation._settle
    crashed = []

    async def crash_once(self, lease, rid, status, tokens):
        if not crashed:
            crashed.append(rid)
            raise RuntimeError("worker died between staging and settlement")
        return await real(self, lease, rid, status, tokens)

    monkeypatch.setattr(EmbedOperation, "_settle", crash_once)
    with pytest.raises(RuntimeError):
        await operation.execute(lease_for(case))
    calls_before = len(embedder.calls)
    assert await scalar(migration_factory, "SELECT count(*) FROM embed_vector_stage") >= 1
    monkeypatch.setattr(EmbedOperation, "_settle", real)
    receipt = await operation.execute(lease_for(case))
    # the staged chunk was settled from its staged result: the provider saw each chunk once
    assert len(embedder.calls) == receipt.chunk_count and calls_before >= 1
    assert (
        await scalar(
            migration_factory,
            "SELECT count(*) FROM embedding_reservations WHERE state='committed'",
        )
        == receipt.chunk_count
    )


async def test_a_failed_provider_call_leaves_an_uncertain_hold_and_is_never_resent(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    embedder = Embedder(fail=ProviderTransportError("provider_timeout"))
    operation = EmbedOperation(broker_factory, embedder, concurrency=1)
    with pytest.raises(EmbedFailure) as error:
        await operation.execute(lease_for(case))
    assert error.value.code == "embed_provider_timeout" and error.value.transient
    assert (
        await scalar(
            migration_factory,
            "SELECT count(*) FROM embedding_reservations WHERE state='uncertain_hold'",
        )
        == 1
    )
    calls = len(embedder.calls)
    with pytest.raises(EmbedFailure) as again:  # the held chunk is not retried automatically
        await EmbedOperation(broker_factory, Embedder(), concurrency=1).execute(lease_for(case))
    assert again.value.code == "embed_hold_pending" and not again.value.transient
    assert calls == 1
    assert (
        await scalar(
            migration_factory, "SELECT count(*) FROM search_indices WHERE index_type='dense'"
        )
        == 0
    )


async def test_a_stale_fence_cannot_embed_or_publish(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    embedder = Embedder()
    with pytest.raises(StaleAttempt):
        await EmbedOperation(broker_factory, embedder).execute(lease_for(case, fence=2))
    assert embedder.calls == []
    assert await scalar(migration_factory, "SELECT count(*) FROM embed_spend_ledger") == 0

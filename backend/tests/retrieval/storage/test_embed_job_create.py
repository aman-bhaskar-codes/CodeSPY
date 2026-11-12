"""EmbedJobService.create/get/cancel against real PostgreSQL authority."""

import os
from uuid import uuid4

import pytest
from app.contracts.embedding_index import EmbedConfigV1
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.services.embed_job import EmbedOperation
from app.services.embed_jobs import EmbedJobService, EmbedUnavailable
from sqlalchemy import text

from tests.retrieval.storage.test_dense_guards import MODEL, PROVIDER, embed_base, embed_case
from tests.retrieval.storage.test_dense_job import Embedder, lease_for

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0046)",
)


def config(**overrides):
    values = {
        "provider": PROVIDER,
        "model_id": MODEL,
        "dimension": 512,
        "normalize": True,
        "price_version": "test",
    }
    return EmbedConfigV1(**{**values, **overrides})


async def count(migration_factory, query, **params):
    async with migration_factory() as session:
        return await session.scalar(text(query), params)


async def test_create_stores_an_immutable_spec_and_enqueues_once_per_key(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    ctx, spec = base["ctx"], base["spec"]
    service = EmbedJobService(api_factory)
    args = (ctx, spec.repository_id, spec.snapshot_id, base["set_id"], config())
    first = await service.create(*args, "create-1")
    assert first.job.state == "queued" and first.job.operation == "embed_chunk_set"
    replay = await service.create(*args, "create-1")
    assert replay.job.id == first.job.id
    with pytest.raises(DuplicateConflict):  # same key, different profile
        await service.create(
            ctx,
            spec.repository_id,
            spec.snapshot_id,
            base["set_id"],
            config(dimension=256),
            "create-1",
        )
    second = await service.create(*args, "create-2")  # a new request for the same profile
    assert second.job.id != first.job.id and second.job.embed_spec_id == first.job.embed_spec_id
    specs = await count(
        migration_factory,
        "SELECT count(*) FROM embed_specs WHERE org_id=:o AND dimension=512",
        o=ctx.org_id,
    )
    assert specs == 1
    wakeups = await count(
        migration_factory,
        "SELECT count(*) FROM outbox WHERE org_id=:o AND job_id=:j",
        o=ctx.org_id,
        j=first.job.id,
    )
    assert wakeups == 1


async def test_requests_that_cannot_be_served_are_refused_before_any_row_exists(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    ctx, spec = base["ctx"], base["spec"]
    service = EmbedJobService(api_factory)
    with pytest.raises(ScopeViolation):  # a chunk set that does not exist in this snapshot
        await service.create(ctx, spec.repository_id, spec.snapshot_id, uuid4(), config(), "k1")
    with pytest.raises(EmbedUnavailable) as unpriced:
        await service.create(
            ctx,
            spec.repository_id,
            spec.snapshot_id,
            base["set_id"],
            config(price_version="none"),
            "k2",
        )
    assert unpriced.value.code == "embed_price_unavailable"
    with pytest.raises(ValueError):
        await service.create(
            ctx, spec.repository_id, spec.snapshot_id, base["set_id"], config(dimension=300), "k3"
        )
    jobs = await count(
        migration_factory, "SELECT count(*) FROM jobs WHERE operation='embed_chunk_set'"
    )
    assert jobs == 0
    specs = await count(
        migration_factory,
        "SELECT count(*) FROM embed_specs WHERE org_id=:o AND dimension=512",
        o=ctx.org_id,
    )
    assert specs == 0


async def test_get_reports_status_and_cancel_ends_a_queued_job(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    ctx, spec = base["ctx"], base["spec"]
    service = EmbedJobService(api_factory)
    created = await service.create(
        ctx, spec.repository_id, spec.snapshot_id, base["set_id"], config(), "status-1"
    )
    status = await service.get(ctx, spec.repository_id, spec.snapshot_id, created.job.id)
    assert status.job.state == "queued" and status.receipt is None and status.can_cancel
    with pytest.raises(ScopeViolation):  # wrong snapshot scope
        await service.get(ctx, spec.repository_id, uuid4(), created.job.id)
    cancelled = await service.cancel(ctx, spec.repository_id, created.job.id)
    assert cancelled.job.state == "cancelled" and not cancelled.can_cancel


async def test_an_already_published_profile_is_refused_and_status_carries_the_receipt(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    ctx, spec = case["ctx"], case["spec"]
    receipt = await EmbedOperation(broker_factory, Embedder()).execute(lease_for(case))
    service = EmbedJobService(api_factory)
    with pytest.raises(DuplicateConflict):
        await service.create(
            ctx,
            spec.repository_id,
            spec.snapshot_id,
            case["set_id"],
            config(dimension=256),
            "again",
        )
    status = await service.get(ctx, spec.repository_id, spec.snapshot_id, case["binding"][0])
    assert status.job.state == "succeeded" and status.receipt is not None
    assert status.receipt.index_id == receipt.index_id and not status.can_cancel

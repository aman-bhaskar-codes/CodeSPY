# ruff: noqa: E501
"""Queue consumer runs embed jobs end to end and maps failures to durable job outcomes."""

import json
import os

import pytest
from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.embedding_index import EmbedConfigV1
from app.domain.canonical_json import canonical_bytes
from app.ports.queue import Delivery
from app.services.embed_job import EmbedOperation
from app.services.embed_jobs import EmbedJobService
from app.workers.consumer import JobConsumer
from sqlalchemy import text

from tests.retrieval.storage.test_dense_guards import (
    ALLOW,
    EMBED_POLICY,
    MODEL,
    PROVIDER,
    embed_base,
)
from tests.retrieval.storage.test_dense_job import Embedder

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0046)",
)


class Queue:
    def __init__(self):
        self.acked, self.extended = 0, 0

    async def ack(self, receipt):
        self.acked += 1

    async def extend(self, *args):
        self.extended += 1


async def created(base, api_factory, key="consume"):
    spec = base["spec"]
    return await EmbedJobService(api_factory).create(
        base["ctx"],
        spec.repository_id,
        spec.snapshot_id,
        base["set_id"],
        EmbedConfigV1(
            provider=PROVIDER, model_id=MODEL, dimension=512, normalize=True, price_version="test"
        ),
        key,
        policy=EMBED_POLICY,
    )


async def wakeup(migration_factory, job_id, generation=1):
    async with migration_factory() as session:
        payload = await session.scalar(
            text("SELECT payload FROM outbox WHERE job_id=:j AND generation=:g"),
            {"j": job_id, "g": generation},
        )
    return json.loads(payload) if isinstance(payload, str) else payload


def delivery(payload):
    return Delivery(canonical_bytes(payload), "delivery")


async def state(migration_factory, job_id):
    async with migration_factory() as session:
        row = (
            await session.execute(
                text("SELECT state,failure_code,dispatch_generation FROM jobs WHERE id=:j"),
                {"j": job_id},
            )
        ).one()
    return row.state, row.failure_code, row.dispatch_generation


async def test_consumer_runs_the_job_and_acks_only_after_a_durable_receipt(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    job = (await created(base, api_factory)).job
    payload = await wakeup(migration_factory, job.id)
    embedder = Embedder()
    queue = Queue()
    consumer = JobConsumer(
        broker_factory,
        queue,
        None,
        None,
        embed_operation=EmbedOperation(broker_factory, embedder, policy_for=ALLOW),
    )
    await consumer.handle(delivery(payload))
    assert queue.acked == 1 and embedder.calls
    assert (await state(migration_factory, job.id))[0] == "succeeded"
    status = await EmbedJobService(api_factory).get(
        base["ctx"], base["spec"].repository_id, base["spec"].snapshot_id, job.id
    )
    assert status.receipt is not None and status.receipt.chunk_count == len(embedder.calls)
    await consumer.handle(delivery(payload))  # a duplicate wakeup for a finished job is acked
    assert queue.acked == 2 and len(embedder.calls) == status.receipt.chunk_count


async def test_unconfigured_consumer_defers_without_claiming(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    job = (await created(base, api_factory)).job
    queue = Queue()
    await JobConsumer(broker_factory, queue, None, None).handle(
        delivery(await wakeup(migration_factory, job.id))
    )
    assert (queue.acked, queue.extended) == (0, 1)
    assert (await state(migration_factory, job.id))[0] == "queued"


async def test_a_wakeup_that_does_not_match_the_stored_spec_is_acked_and_ignored(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    job = (await created(base, api_factory)).job
    payload = await wakeup(migration_factory, job.id)
    forged = {**payload, "embed_spec_id": str(base["ctx"].org_id)}
    embedder, queue = Embedder(), Queue()
    await JobConsumer(
        broker_factory,
        queue,
        None,
        None,
        embed_operation=EmbedOperation(broker_factory, embedder, policy_for=ALLOW),
    ).handle(delivery(forged))
    assert queue.acked == 1 and embedder.calls == []
    assert (await state(migration_factory, job.id))[0] == "queued"


async def test_transient_provider_failure_retries_and_a_held_chunk_fails_the_job(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    job = (await created(base, api_factory)).job
    queue = Queue()
    failing = Embedder(fail=ProviderTransportError("provider_timeout"))
    consumer = JobConsumer(
        broker_factory,
        queue,
        None,
        None,
        embed_operation=EmbedOperation(broker_factory, failing, policy_for=ALLOW, concurrency=1),
    )
    await consumer.handle(delivery(await wakeup(migration_factory, job.id)))
    assert queue.acked == 1
    assert await state(migration_factory, job.id) == ("retry_wait", "embed_provider_timeout", 2)
    async with migration_factory() as session, session.begin():
        await session.execute(
            text("UPDATE jobs SET not_before=clock_timestamp()-interval '1 second' WHERE id=:j"),
            {"j": job.id},
        )
    healthy = Embedder()
    consumer.embed_operation = EmbedOperation(
        broker_factory, healthy, policy_for=ALLOW, concurrency=1
    )
    await consumer.handle(delivery(await wakeup(migration_factory, job.id, generation=2)))
    # the first attempt's call left an uncertain hold: the job fails, the provider is not asked again
    assert await state(migration_factory, job.id) == ("failed", "embed_hold_pending", 2)
    assert healthy.calls == [] and queue.acked == 2

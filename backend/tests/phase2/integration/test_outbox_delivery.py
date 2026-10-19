from app.adapters.queue.memory import MemoryQueue
from app.admission.zip_inventory import inspect_zip_bytes
from app.models.phase2_tables import PHASE2_TABLES
from app.services.job_finalization import JobFinalizer
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.runtime import WorkerRuntime
from sqlalchemy import func, select
from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario


async def test_duplicate_queue_delivery_has_one_admission_effect(factory, tmp_path):
    svc, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await svc.write_content(ctx, sub.id, chunks(body))
    queue = MemoryQueue()
    dispatcher = OutboxDispatcher(factory, queue)
    assert await dispatcher.dispatch_once() == 1
    delivery = (await queue.receive(1, wait_seconds=0))[0]
    await queue.send(delivery.body)

    class FixtureProcessor:
        async def inspect(self, lease, policy):
            return inspect_zip_bytes(body, policy)

    consumer = JobConsumer(factory, queue, JobFinalizer(factory, store), FixtureProcessor())
    runtime = WorkerRuntime(dispatcher, consumer)
    await runtime.handle(delivery)
    duplicate = (await queue.receive(1, wait_seconds=0))[0]
    await consumer.handle(duplicate)
    async with factory() as s:
        assert (
            await s.scalar(
                select(PHASE2_TABLES["jobs"].c.state).where(
                    PHASE2_TABLES["jobs"].c.id == uploaded.job_id
                )
            )
            == "succeeded"
        )
        assert (
            await s.scalar(
                select(func.count())
                .select_from(PHASE2_TABLES["jobs"])
                .where(PHASE2_TABLES["jobs"].c.parent_job_id == uploaded.job_id)
            )
            == 1
        )

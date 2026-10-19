import os

import pytest
from app.adapters.queue.memory import MemoryQueue
from app.admission.process import DockerAdmissionRunner
from app.models.phase2_tables import PHASE2_TABLES
from app.services.job_finalization import JobFinalizer
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.operations.admit_source import StoredAdmissionProcessor
from sqlalchemy import select
from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario


async def test_stored_input_passes_linux_admission_and_inventory(factory, tmp_path):
    image = os.environ.get("CODESPY_ADMISSION_IMAGE")
    if not image:
        pytest.skip("owned admission image required")
    service, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await service.write_content(ctx, sub.id, chunks(body))
    queue = MemoryQueue()
    dispatcher = OutboxDispatcher(factory, queue)
    processor = StoredAdmissionProcessor(
        factory, store, DockerAdmissionRunner(image, tmp_path / "runner")
    )
    consumer = JobConsumer(factory, queue, JobFinalizer(factory, store), processor)
    for _ in range(2):
        assert await dispatcher.dispatch_once() == 1
        await consumer.handle((await queue.receive(1, wait_seconds=0))[0])
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(PHASE2_TABLES["jobs"]).where(
                        PHASE2_TABLES["jobs"].c.org_id == ctx.org_id
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2 and {row["state"] for row in rows} == {"succeeded"}
        assert next(row for row in rows if row["id"] == uploaded.job_id)["snapshot_id"] is not None

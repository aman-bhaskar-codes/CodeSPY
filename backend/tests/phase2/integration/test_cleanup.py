from datetime import timedelta

from app.models.phase2_tables import PHASE2_TABLES
from app.workers.cleanup import CleanupWorker
from app.workers.recovery import RecoveryWorker
from sqlalchemy import func, select, update
from tests.phase2.integration.test_submission_ingestion import submission_scenario


async def test_abandoned_reservation_released_once_and_exact_cleanup(factory, tmp_path):
    _service, ctx, _, sub, _body, store = await submission_scenario(factory, tmp_path)
    table = PHASE2_TABLES["source_submissions"]
    async with factory.begin() as session:
        await session.execute(
            update(table)
            .where(table.c.id == sub.id)
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    recovery = RecoveryWorker(factory)
    assert await recovery.tick() >= 1
    await recovery.tick()
    async with factory() as session:
        current = (
            (await session.execute(select(table).where(table.c.id == sub.id))).mappings().one()
        )
        assert current["state"] == "expired" and current["reservation_released_at"] is not None
        assert (
            await session.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
                )
            )
            == 0
        )
    # A ledger task with no bytes is also an idempotent cleanup operation.
    cleanup = CleanupWorker(factory, store)
    assert await cleanup.tick() >= 1
    await cleanup.tick()


async def test_admitted_quarantine_expires_while_canonical_source_remains(factory, tmp_path):
    from app.admission.zip_inventory import inspect_zip_bytes
    from app.contracts.ingestion import AdmissionPolicyV1
    from app.services.job_finalization import JobFinalizer
    from app.services.jobs import JobService
    from tests.phase2.integration.test_submission_ingestion import chunks

    service, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await service.write_content(ctx, sub.id, chunks(body))
    lease = await JobService(factory).claim(ctx.org_id, uploaded.job_id, 1, "fixture")
    finalizer = JobFinalizer(factory, store)
    prepared = await finalizer.prepare_admission(
        lease, inspect_zip_bytes(body, AdmissionPolicyV1())
    )
    await finalizer.finish_admission(lease, prepared)
    objects = PHASE2_TABLES["object_records"]
    async with factory.begin() as s:
        await s.execute(
            update(objects)
            .where(objects.c.org_id == ctx.org_id, objects.c.kind == "input")
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    await RecoveryWorker(factory).tick()
    await CleanupWorker(factory, store).tick()
    async with factory() as s:
        assert (
            await s.scalar(
                select(objects.c.state).where(
                    objects.c.org_id == ctx.org_id, objects.c.kind == "input"
                )
            )
            == "deleted"
        )
    assert await store.describe_owned(prepared.source.key, 104857600) == prepared.source


async def test_deleted_orphan_can_start_a_new_verified_intent(factory, tmp_path):
    from app.admission.zip_inventory import inspect_zip_bytes
    from app.contracts.ingestion import AdmissionPolicyV1
    from app.services.job_finalization import JobFinalizer
    from app.services.jobs import JobService
    from tests.phase2.integration.test_submission_ingestion import chunks

    service, ctx, request, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await service.write_content(ctx, sub.id, chunks(body))
    lifecycle = JobService(factory)
    first = await lifecycle.claim(ctx.org_id, uploaded.job_id, 1, "first")
    finalizer = JobFinalizer(factory, store)
    manifest = inspect_zip_bytes(body, AdmissionPolicyV1())
    await finalizer.prepare_admission(first, manifest)
    await lifecycle.cancel(ctx, uploaded.job_id)
    objects = PHASE2_TABLES["object_records"]
    async with factory.begin() as s:
        await s.execute(
            update(objects)
            .where(objects.c.org_id == ctx.org_id, objects.c.kind.in_(["source", "manifest"]))
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    await RecoveryWorker(factory).tick()
    await CleanupWorker(factory, store).tick()
    new = await service.reserve(ctx, request, "new-after-orphan-cleanup")
    new = await service.write_content(ctx, new.id, chunks(body))
    lease = await lifecycle.claim(ctx.org_id, new.job_id, 1, "second")
    prepared = await finalizer.prepare_admission(lease, manifest)
    assert (await finalizer.finish_admission(lease, prepared)).state == "succeeded"

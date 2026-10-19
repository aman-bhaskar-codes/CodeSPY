import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.jobs import AttemptLeaseV2
from app.models.phase2_tables import PHASE2_TABLES
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from sqlalchemy import func, select
from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario


async def test_admission_inventory_commit_and_replay_are_atomic(factory, tmp_path):
    svc, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await svc.write_content(ctx, sub.id, chunks(body))
    jobs = JobService(factory)
    lease = await jobs.claim(ctx.org_id, uploaded.job_id, 1, "fixture")
    assert isinstance(lease, AttemptLeaseV2) and lease.run_ref is None
    manifest = inspect_zip_bytes(body, AdmissionPolicyV1())
    finalizer = JobFinalizer(factory, store)
    prepared = await finalizer.prepare_admission(lease, manifest)
    result = await finalizer.finish_admission(lease, prepared)
    assert result.state == "succeeded" and result.snapshot_id is not None
    assert (await finalizer.finish_admission(lease, prepared)).id == result.id
    async with factory() as s:
        children = (
            await s.scalars(
                select(PHASE2_TABLES["jobs"].c.id).where(
                    PHASE2_TABLES["jobs"].c.parent_job_id == result.id
                )
            )
        ).all()
        assert len(children) == 1
        assert (
            await s.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
                )
            )
            == 0
        )
        assert (
            await s.scalar(
                select(PHASE2_TABLES["source_submissions"].c.state).where(
                    PHASE2_TABLES["source_submissions"].c.id == sub.id
                )
            )
            == "admitted"
        )
    child = await jobs.claim(ctx.org_id, children[0], 1, "inventory")
    assert isinstance(child, AttemptLeaseV2) and child.run_ref
    batch = await finalizer.inventory_batch(child)
    done = await finalizer.finish_inventory(child, batch)
    assert done.state == "succeeded" and done.result_run_id == child.run_ref.run_id
    assert (await finalizer.finish_inventory(child, batch)).result_run_id == done.result_run_id


async def test_lost_commit_ack_reads_durable_outcome(factory, tmp_path):
    svc, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await svc.write_content(ctx, sub.id, chunks(body))
    lease = await JobService(factory).claim(ctx.org_id, uploaded.job_id, 1, "fixture")
    assert isinstance(lease, AttemptLeaseV2)

    class LostAck(JobFinalizer):
        async def _commit(self, session):
            await session.commit()
            raise ConnectionError("synthetic lost acknowledgement")

    finalizer = LostAck(factory, store)
    prepared = await finalizer.prepare_admission(
        lease, inspect_zip_bytes(body, AdmissionPolicyV1())
    )
    assert (await finalizer.finish_admission(lease, prepared)).state == "succeeded"
    async with factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(PHASE2_TABLES["jobs"])
                .where(PHASE2_TABLES["jobs"].c.parent_job_id == lease.job_id)
            )
            == 1
        )


async def test_concurrent_identical_admissions_use_one_stored_file_mapping(factory, tmp_path):
    import asyncio

    from app.models.tables import source_files

    svc, ctx, request, first, body, store = await submission_scenario(factory, tmp_path)
    second = await svc.reserve(ctx, request, "second-archive")
    a = await svc.write_content(ctx, first.id, chunks(body))
    b = await svc.write_content(ctx, second.id, chunks(body))
    jobs = JobService(factory)
    finalizer = JobFinalizer(factory, store)
    la, lb = await asyncio.gather(
        jobs.claim(ctx.org_id, a.job_id, 1, "one"), jobs.claim(ctx.org_id, b.job_id, 1, "two")
    )
    assert isinstance(la, AttemptLeaseV2) and isinstance(lb, AttemptLeaseV2)
    manifest = inspect_zip_bytes(body, AdmissionPolicyV1())
    pa, pb = await asyncio.gather(
        finalizer.prepare_admission(la, manifest), finalizer.prepare_admission(lb, manifest)
    )
    ra, rb = await asyncio.gather(
        finalizer.finish_admission(la, pa), finalizer.finish_admission(lb, pb)
    )
    assert ra.snapshot_id == rb.snapshot_id
    async with factory() as s:
        stored = (
            await s.scalars(
                select(source_files.c.id).where(
                    source_files.c.org_id == ctx.org_id,
                    source_files.c.snapshot_id == ra.snapshot_id,
                )
            )
        ).all()
        assert len(stored) == 1
        associated = (
            await s.scalars(
                select(PHASE2_TABLES["inventory_entries"].c.file_id).where(
                    PHASE2_TABLES["inventory_entries"].c.org_id == ctx.org_id,
                    PHASE2_TABLES["inventory_entries"].c.snapshot_id == ra.snapshot_id,
                )
            )
        ).all()
        assert associated == stored
        assert (
            await s.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
                )
            )
            == 0
        )


async def test_conflicting_eligibility_cannot_append_to_sealed_inventory(factory, tmp_path):
    from app.domain.errors import DuplicateConflict

    svc, ctx, request, first, body, store = await submission_scenario(factory, tmp_path)
    jobs = JobService(factory)
    finalizer = JobFinalizer(factory, store)
    a = await svc.write_content(ctx, first.id, chunks(body))
    la = await jobs.claim(ctx.org_id, a.job_id, 1, "one")
    assert isinstance(la, AttemptLeaseV2)
    manifest = inspect_zip_bytes(body, AdmissionPolicyV1())
    accepted = await finalizer.finish_admission(la, await finalizer.prepare_admission(la, manifest))
    second = await svc.reserve(ctx, request, "conflicting-eligibility")
    b = await svc.write_content(ctx, second.id, chunks(body))
    lb = await jobs.claim(ctx.org_id, b.job_id, 1, "two")
    assert isinstance(lb, AttemptLeaseV2)
    conflict = manifest.model_copy(
        update={
            "entries": [
                e.model_copy(update={"eligible": False, "reason": "unsupported_language"})
                for e in manifest.entries
            ]
        }
    )
    prepared = await finalizer.prepare_admission(lb, conflict)
    with pytest.raises(DuplicateConflict):
        await finalizer.finish_admission(lb, prepared)
    await jobs.fail_attempt(lb, "integrity_failure")
    async with factory() as s:
        assert (
            await s.scalar(
                select(PHASE2_TABLES["inventory_entries"].c.eligible).where(
                    PHASE2_TABLES["inventory_entries"].c.org_id == ctx.org_id,
                    PHASE2_TABLES["inventory_entries"].c.snapshot_id == accepted.snapshot_id,
                )
            )
            is True
        )
        assert (
            await s.scalar(
                select(PHASE2_TABLES["source_submissions"].c.state).where(
                    PHASE2_TABLES["source_submissions"].c.id == second.id
                )
            )
            == "rejected"
        )
        assert (
            await s.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
                )
            )
            == 0
        )


async def test_slow_external_verification_precedes_fresh_lease_entry(factory, tmp_path):
    from sqlalchemy import text

    service, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await service.write_content(ctx, sub.id, chunks(body))
    lifecycle = JobService(factory)
    lease = await lifecycle.claim(ctx.org_id, uploaded.job_id, 1, "fixture")

    class SlowRead(JobFinalizer):
        async def _verify(self, obj):
            await super()._verify(obj)
            # Coordinated DB-time fixture represents budget consumed by I/O,
            # without arbitrary sleeps or replacing production clock predicates.
            async with factory.begin() as s:
                await s.execute(
                    text(
                        "UPDATE job_attempts SET "
                        "lease_expires_at=clock_timestamp()+interval '62 seconds' "
                        "WHERE org_id=:org AND id=:attempt"
                    ),
                    {"org": ctx.org_id, "attempt": lease.attempt_id},
                )

    finalizer = SlowRead(factory, store)
    prepared = await finalizer.prepare_admission(
        lease, inspect_zip_bytes(body, AdmissionPolicyV1())
    )
    called = []

    async def enter():
        await lifecycle.heartbeat(lease)
        called.append(True)

    assert (
        await finalizer.finish_admission(lease, prepared, before_lock=enter)
    ).state == "succeeded"
    assert called == [True]


async def test_hard_transport_fallback_releases_pool_slot_and_database_lock(factory, tmp_path):
    import asyncio

    from app.adapters.storage.local import LocalObjectStore
    from app.domain.errors import FinalizationOutcomeUnknown
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(
        factory.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=0.1
    )
    sessions = []

    class HungCleanup(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

        async def rollback(self):
            await asyncio.Event().wait()

        async def invalidate(self):
            await asyncio.Event().wait()

    finalizer = JobFinalizer(
        async_sessionmaker(engine, class_=HungCleanup),
        LocalObjectStore(tmp_path / "objects"),
        cleanup_seconds=0.05,
        safety_seconds=0.1,
    )

    async def work(session, _budget):
        await session.execute(text("SELECT pg_advisory_xact_lock(726521)"))
        raise ValueError("fixture forward failure")

    async def resolve():
        raise AssertionError("no commit attempted")

    try:
        with pytest.raises(FinalizationOutcomeUnknown):
            await finalizer._execute(work, resolve)
        assert engine.pool.checkedout() == 0
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT pg_try_advisory_xact_lock(726521)"))
    finally:
        from contextlib import suppress

        from sqlalchemy.exc import DBAPIError

        for session in sessions:
            with suppress(
                DBAPIError
            ):  # The deliberately terminated driver's rollback is unavailable.
                await AsyncSession.close(session)
        await engine.dispose()

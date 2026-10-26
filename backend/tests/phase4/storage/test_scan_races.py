"""Database-time authority and rollback checks on independently owned transactions."""

import pytest
from app.domain.errors import StaleAttempt
from app.models.scan_tables import (
    finding_occurrences,
    finding_support,
    scan_reports,
    verification_decisions,
)
from app.models.tables import evidence_records
from app.services.jobs import JobService
from app.services.scan_publication import FindingFinalizer
from scripts import phase2_db as harness
from sqlalchemy import func, select

from tests.phase4.storage.test_scan_publication import publication_case


async def assert_no_publication(factory):
    async with factory() as session:
        for table in (
            scan_reports,
            finding_occurrences,
            finding_support,
            verification_decisions,
            evidence_records,
        ):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def test_actual_database_expiry_blocks_stale_publication(
    api_factory, broker_factory, migration_factory, tmp_path, db_values
):
    ctx, _, job, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    with harness.connect(db_values, db_values["CODESPY_TEST_DATABASE"]) as connection:
        connection.execute(
            "UPDATE job_attempts SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE org_id=%s AND job_id=%s AND id=%s",
            (ctx.org_id, job.id, lease.attempt_id),
        )
    with pytest.raises(StaleAttempt):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)
    assert await JobService(broker_factory).recover_due() == 1
    assert (await JobService(broker_factory).get(ctx, job.id)).state == "retry_wait"


async def test_failure_after_projection_inserts_rolls_back_every_result(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    from app.repositories.evidence_repository import EvidenceRepository

    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )

    async def fail(*args):
        raise RuntimeError("owned transaction fault")

    monkeypatch.setattr(EvidenceRepository, "insert_batch", fail)
    with pytest.raises(RuntimeError, match="owned transaction fault"):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)


async def test_sql_rejects_same_count_but_mutated_occurrence_payload(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    from app.services import scan_publication

    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    original = scan_publication.encoded

    def changed(payload):
        if "title" in payload:
            payload = {**payload, "title": "tampered"}
        return original(payload)

    monkeypatch.setattr(scan_publication, "encoded", changed)
    with pytest.raises(Exception, match="finding set differs"):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)


async def test_lost_commit_acknowledgement_recovers_exact_durable_result(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    finalizer = FindingFinalizer(broker_factory)

    async def lost_ack(session):
        await session.commit()
        raise ConnectionError("synthetic lost commit acknowledgement")

    finalizer.executor.commit_hook = lost_ack
    assert await finalizer.finish(lease, draft) == draft


async def test_cancellation_after_commit_stays_cancelled_and_durable_state_survives(
    api_factory, broker_factory, migration_factory, tmp_path
):
    import asyncio

    ctx, _, job, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    finalizer = FindingFinalizer(broker_factory)

    async def cancelled_ack(session):
        await session.commit()
        raise asyncio.CancelledError()

    finalizer.executor.commit_hook = cancelled_ack
    with pytest.raises(asyncio.CancelledError):
        await finalizer.finish(lease, draft)
    assert (await JobService(broker_factory).get(ctx, job.id)).state == "succeeded"


async def test_multiple_short_statements_exceed_total_budget_and_recover_connection(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    from app.repositories.evidence_repository import EvidenceRepository
    from sqlalchemy import text

    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    original = EvidenceRepository.insert_batch

    async def slow(self, *args):
        for _ in range(20):
            await self.session.execute(text("SELECT pg_sleep(0.03)"))
        return await original(self, *args)

    monkeypatch.setattr(EvidenceRepository, "insert_batch", slow)
    with pytest.raises(TimeoutError):
        await FindingFinalizer(broker_factory, budget_seconds=0.2).finish(lease, draft)
    await assert_no_publication(broker_factory)
    async with broker_factory() as session:
        assert await session.scalar(text("SELECT 1")) == 1


async def test_cancellation_waits_for_publication_lock_and_observes_durable_success(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    import asyncio

    from app.repositories.evidence_repository import EvidenceRepository

    ctx, _, job, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    entered, release, cancel_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = EvidenceRepository.insert_batch

    async def barrier(self, *args):
        entered.set()
        await release.wait()
        return await original(self, *args)

    monkeypatch.setattr(EvidenceRepository, "insert_batch", barrier)

    async def cancel():
        cancel_started.set()
        return await JobService(broker_factory).request_cancel(ctx, job.id)

    publication = asyncio.create_task(FindingFinalizer(broker_factory).finish(lease, draft))
    cancellation = None
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            cancellation = asyncio.create_task(cancel())
            await cancel_started.wait()
            assert not cancellation.done()
            release.set()
            assert await publication == draft
            assert (await cancellation).state == "succeeded"
    finally:
        release.set()
        for task in (publication, cancellation):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (publication, cancellation) if t is not None), return_exceptions=True
        )


async def test_cancellation_before_publication_leaves_no_result_and_recovery_closes_run(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, _, job, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    assert (await JobService(broker_factory).request_cancel(ctx, job.id)).state == "cancelling"
    with pytest.raises(StaleAttempt):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)
    from app.workers.recovery import RecoveryWorker

    await RecoveryWorker(broker_factory).tick()
    assert (await JobService(broker_factory).get(ctx, job.id)).state == "cancelled"


async def test_revocation_before_publication_prevents_results_and_recovery_terminalizes(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.domain.errors import ScopeViolation
    from app.models.phase2_tables import PHASE2_TABLES
    from sqlalchemy import update

    ctx, _, job, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(ScopeViolation):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)
    from app.workers.recovery import RecoveryWorker

    await RecoveryWorker(broker_factory).tick()
    from app.repositories.job_repository import load_job

    async with broker_factory() as session:
        row = await load_job(session, ctx.org_id, job.id)
        assert row["state"] == "cancelled" and row["failure_code"] == "access_revoked"

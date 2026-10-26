from datetime import timedelta

from app.models.phase2_tables import PHASE2_TABLES
from app.workers.retention import RetentionWorker
from sqlalchemy import func, select, update

from tests.phase2.integration.test_job_lifecycle import job_scenario


async def test_retention_keeps_terminal_event_and_advances_cursor_floor(factory):
    service, ctx, _, _, job = await job_scenario(factory)
    await service.cancel(ctx, job.id)
    events = PHASE2_TABLES["job_events"]
    async with factory.begin() as s:
        await s.execute(
            update(events)
            .where(events.c.job_id == job.id)
            .values(created_at=func.clock_timestamp() - timedelta(days=8))
        )
    assert await RetentionWorker(factory).tick() >= 1
    async with factory() as s:
        rows = (await s.execute(select(events).where(events.c.job_id == job.id))).mappings().all()
        assert len(rows) == 1 and rows[0]["event_type"] == "cancelled"
        assert (
            await s.scalar(
                select(PHASE2_TABLES["jobs"].c.events_min_sequence).where(
                    PHASE2_TABLES["jobs"].c.id == job.id
                )
            )
            == rows[0]["sequence"]
        )


async def test_retention_progresses_beyond_one_hundred_terminal_histories(factory):
    from uuid import uuid4

    from sqlalchemy import insert

    _, ctx, _, _, original = await job_scenario(factory)
    jobs, events = (PHASE2_TABLES[name] for name in ["jobs", "job_events"])
    async with factory.begin() as s:
        for _ in range(110):
            identity = uuid4()
            await s.execute(
                insert(jobs).values(
                    org_id=ctx.org_id,
                    id=identity,
                    repository_id=original.repository_id,
                    principal_id=ctx.principal_id,
                    operation="admit_source",
                    request_key=identity.hex,
                    request_hash="a" * 64,
                    config_hash="b" * 64,
                    deadline=func.clock_timestamp() + timedelta(minutes=15),
                    event_sequence=2,
                )
            )
            await s.execute(
                insert(events),
                [
                    {
                        "org_id": ctx.org_id,
                        "job_id": identity,
                        "sequence": n,
                        "event_type": "queued",
                    }
                    for n in (1, 2)
                ],
            )
    async with factory.begin() as s:
        await s.execute(
            update(events)
            .where(events.c.org_id == ctx.org_id)
            .values(created_at=func.clock_timestamp() - timedelta(days=8))
        )
    worker = RetentionWorker(factory)
    assert await worker.tick() == 100
    assert await worker.tick() == 10


async def test_expired_auth_and_webhook_metadata_is_bounded_and_removed(factory):
    from uuid import uuid4

    from sqlalchemy import insert

    _, ctx, _, _, _ = await job_scenario(factory)
    async with factory.begin() as s:
        await s.execute(
            insert(PHASE2_TABLES["oauth_flows"]).values(
                id=uuid4(),
                state_hash="a" * 64,
                browser_hash="b" * 64,
                verifier_cipher=b"fixture",
                key_id="fixture",
                return_path="/",
                expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
        await s.execute(
            insert(PHASE2_TABLES["sessions"]).values(
                id=uuid4(),
                principal_id=ctx.principal_id,
                token_hash="c" * 64,
                csrf_hash="d" * 64,
                issued_at=func.clock_timestamp() - timedelta(days=2),
                last_seen_at=func.clock_timestamp() - timedelta(days=2),
                idle_expires_at=func.clock_timestamp() - timedelta(days=1),
                absolute_expires_at=func.clock_timestamp() - timedelta(days=1),
            )
        )
        await s.execute(
            insert(PHASE2_TABLES["webhook_receipts"]).values(
                delivery_id="expired",
                event_type="installation",
                body_sha256="e" * 64,
                state="processed",
                received_at=func.clock_timestamp() - timedelta(days=8),
            )
        )
    await RetentionWorker(factory).tick()
    async with factory() as s:
        for name in ["oauth_flows", "sessions", "webhook_receipts"]:
            assert await s.scalar(select(func.count()).select_from(PHASE2_TABLES[name])) == 0

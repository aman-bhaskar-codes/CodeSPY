"""Real DB time and actual guarded INSERT boundary; no application clock override."""

import asyncio
from uuid import uuid4

import pytest
from app.models.phase2_tables import PHASE2_TABLES
from app.services.finalization_budget import FinalizationBudget, rollback_or_invalidate
from scripts.phase1_fixture import scenario
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError


async def active_admission(factory):
    _, ctx, snap, _, files = await scenario(factory)
    job, attempt = uuid4(), uuid4()
    jobs, attempts = PHASE2_TABLES["jobs"], PHASE2_TABLES["job_attempts"]
    async with factory() as s, s.begin():
        await s.execute(
            insert(jobs).values(
                org_id=ctx.org_id,
                id=job,
                repository_id=snap.repository_id,
                principal_id=ctx.principal_id,
                operation="admit_source",
                state="queued",
                request_key=uuid4().hex,
                request_hash="a" * 64,
                config_hash="b" * 64,
                fence=0,
                deadline=text("clock_timestamp()+interval '90 seconds'"),
            )
        )
        await s.execute(
            insert(attempts).values(
                org_id=ctx.org_id,
                job_id=job,
                id=attempt,
                fence=1,
                owner="fixture",
                state="active",
                lease_expires_at=text("clock_timestamp()+interval '90 seconds'"),
                heartbeat_at=func.clock_timestamp(),
            )
        )
        await s.execute(
            update(jobs)
            .where(jobs.c.org_id == ctx.org_id, jobs.c.id == job)
            .values(current_attempt_id=attempt, state="running", fence=1)
        )
    return ctx, snap, files, job, attempt


async def worker_context(s, ctx, job, attempt):
    for key, value in [
        ("org_id", ctx.org_id),
        ("job_id", job),
        ("attempt_id", attempt),
        ("fence", 1),
    ]:
        await s.execute(
            text("SELECT set_config(:key,:value,true)"),
            {"key": "codespy." + key, "value": str(value)},
        )


async def test_expired_lease_rejects_at_publication_boundary(factory, engine):
    ctx, snap, files, job, attempt = await active_admission(factory)
    # Owned fixture shrinks expiry relative to actual PostgreSQL time.
    async with factory() as s, s.begin():
        await s.execute(
            text(
                "UPDATE job_attempts SET lease_expires_at=clock_timestamp()"
                "+interval '100 milliseconds' "
                "WHERE org_id=:org AND job_id=:job AND id=:attempt"
            ),
            {"org": ctx.org_id, "job": job, "attempt": attempt},
        )
    expiry_observed = asyncio.Event()

    async def observer():
        async with engine.connect() as conn:
            async with asyncio.timeout(3):
                while not await conn.scalar(  # noqa: ASYNC110 -- bounded real DB-time observation
                    text(
                        "SELECT lease_expires_at<=clock_timestamp() FROM job_attempts "
                        "WHERE org_id=:org AND job_id=:job AND id=:attempt"
                    ),
                    {"org": ctx.org_id, "job": job, "attempt": attempt},
                ):
                    await asyncio.sleep(0.005)
            expiry_observed.set()

    async def stale_writer():
        await expiry_observed.wait()
        with pytest.raises(DBAPIError) as error:
            async with factory() as s, s.begin():
                await worker_context(s, ctx, job, attempt)
                await s.execute(
                    insert(PHASE2_TABLES["inventory_entries"]).values(
                        org_id=ctx.org_id,
                        snapshot_id=snap.id,
                        file_id=files[0],
                        eligible=True,
                        policy_hash="b" * 64,
                    )
                )
        assert getattr(error.value.orig, "sqlstate", None) == "23514"

    await asyncio.gather(observer(), stale_writer())
    async with factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(PHASE2_TABLES["inventory_entries"])
                .where(PHASE2_TABLES["inventory_entries"].c.org_id == ctx.org_id)
            )
            == 0
        )


async def test_cumulative_statements_cancel_and_recover_pool(factory):
    ctx, _, _, job, _ = await active_admission(factory)
    session = factory()
    with pytest.raises(TimeoutError):
        try:
            async with FinalizationBudget(0.15):
                await session.begin()
                await session.execute(text("SET LOCAL statement_timeout='100ms'"))
                await session.execute(
                    insert(PHASE2_TABLES["job_events"]).values(
                        org_id=ctx.org_id,
                        job_id=job,
                        sequence=1,
                        event_type="fixture",
                        public_payload={},
                    )
                )
                for _ in range(3):
                    await session.execute(text("SELECT pg_sleep(0.06)"))
                await session.commit()
        except BaseException:
            await rollback_or_invalidate(session, seconds=1)
            raise
    async with factory() as recovered:
        assert await recovered.scalar(text("SELECT 1")) == 1
        assert (
            await recovered.scalar(
                select(func.count())
                .select_from(PHASE2_TABLES["job_events"])
                .where(PHASE2_TABLES["job_events"].c.org_id == ctx.org_id)
            )
            == 0
        )


async def test_unselected_attempt_cannot_publish(factory):
    ctx, snap, files, job, attempt = await active_admission(factory)
    async with factory() as s, s.begin():
        await s.execute(
            text("UPDATE jobs SET current_attempt_id=NULL WHERE org_id=:org AND id=:job"),
            {"org": ctx.org_id, "job": job},
        )
    with pytest.raises(DBAPIError) as error:
        async with factory() as s, s.begin():
            await worker_context(s, ctx, job, attempt)
            await s.execute(
                insert(PHASE2_TABLES["inventory_entries"]).values(
                    org_id=ctx.org_id,
                    snapshot_id=snap.id,
                    file_id=files[0],
                    eligible=True,
                    policy_hash="b" * 64,
                )
            )
    assert getattr(error.value.orig, "sqlstate", None) == "23514"


async def test_stale_attempt_cannot_seal_existing_building_snapshot(factory):
    from app.models.tables import source_snapshots

    ctx, snap, _, job, attempt = await active_admission(factory)
    candidate = uuid4()
    async with factory() as s, s.begin():
        artifact = await s.scalar(
            select(source_snapshots.c.artifact_id).where(
                source_snapshots.c.org_id == ctx.org_id, source_snapshots.c.id == snap.id
            )
        )
        await s.execute(
            insert(source_snapshots).values(
                org_id=ctx.org_id,
                id=candidate,
                repository_id=snap.repository_id,
                artifact_id=artifact,
                admission_config_hash="b" * 64,
                identity_hash=uuid4().hex * 2,
            )
        )
        await s.execute(
            text(
                "UPDATE job_attempts SET lease_expires_at=clock_timestamp()"
                "-interval '1 millisecond' "
                "WHERE org_id=:org AND job_id=:job AND id=:attempt"
            ),
            {"org": ctx.org_id, "job": job, "attempt": attempt},
        )
    with pytest.raises(DBAPIError) as error:
        async with factory() as s, s.begin():
            await worker_context(s, ctx, job, attempt)
            await s.execute(
                update(source_snapshots)
                .where(source_snapshots.c.org_id == ctx.org_id, source_snapshots.c.id == candidate)
                .values(state="sealed", manifest_hash="a" * 64, sealed_at=func.clock_timestamp())
            )
    assert getattr(error.value.orig, "sqlstate", None) == "23514"


async def test_terminal_job_cannot_be_inserted_without_publication(factory):
    ctx, snap, _, _, _ = await active_admission(factory)
    with pytest.raises(DBAPIError) as error:
        async with factory() as s, s.begin():
            await s.execute(
                insert(PHASE2_TABLES["jobs"]).values(
                    org_id=ctx.org_id,
                    id=uuid4(),
                    repository_id=snap.repository_id,
                    principal_id=ctx.principal_id,
                    operation="admit_source",
                    state="succeeded",
                    request_key=uuid4().hex,
                    request_hash="a" * 64,
                    config_hash="b" * 64,
                    deadline=func.clock_timestamp(),
                    finished_at=func.clock_timestamp(),
                    publication_checked_at=func.clock_timestamp(),
                )
            )
    assert getattr(error.value.orig, "sqlstate", None) == "23514"


async def test_current_context_cannot_create_prior_attempt_run(factory):
    from app.models.tables import analysis_runs

    ctx, snap, _, job, old_attempt = await active_admission(factory)
    current_attempt, old_run = uuid4(), uuid4()
    with pytest.raises(DBAPIError) as error:
        async with factory() as s, s.begin():
            await s.execute(
                update(PHASE2_TABLES["job_attempts"])
                .where(
                    PHASE2_TABLES["job_attempts"].c.org_id == ctx.org_id,
                    PHASE2_TABLES["job_attempts"].c.id == old_attempt,
                )
                .values(
                    state="expired",
                    finished_at=func.clock_timestamp(),
                    run_snapshot_id=snap.id,
                    run_id=old_run,
                )
            )
            await s.execute(
                insert(PHASE2_TABLES["job_attempts"]).values(
                    org_id=ctx.org_id,
                    job_id=job,
                    id=current_attempt,
                    fence=2,
                    owner="fixture-current",
                    state="active",
                    lease_expires_at=text("clock_timestamp()+interval '90 seconds'"),
                    heartbeat_at=func.clock_timestamp(),
                )
            )
            await s.execute(
                update(PHASE2_TABLES["jobs"])
                .where(
                    PHASE2_TABLES["jobs"].c.org_id == ctx.org_id, PHASE2_TABLES["jobs"].c.id == job
                )
                .values(
                    operation="inventory_snapshot",
                    snapshot_id=snap.id,
                    current_attempt_id=current_attempt,
                    fence=2,
                )
            )
            for key, value in [
                ("org_id", ctx.org_id),
                ("job_id", job),
                ("attempt_id", current_attempt),
                ("fence", 2),
            ]:
                await s.execute(
                    text("SELECT set_config(:key,:value,true)"),
                    {"key": "codespy." + key, "value": str(value)},
                )
            await s.execute(
                insert(analysis_runs).values(
                    org_id=ctx.org_id,
                    snapshot_id=snap.id,
                    id=old_run,
                    request_key=uuid4().hex,
                    analyzer_name="inventory-fixture",
                    analyzer_version="1",
                    config={},
                    config_hash="c" * 64,
                    worker_job_id=job,
                    worker_attempt_id=old_attempt,
                    worker_fence=1,
                )
            )
    assert getattr(error.value.orig, "sqlstate", None) == "23514"

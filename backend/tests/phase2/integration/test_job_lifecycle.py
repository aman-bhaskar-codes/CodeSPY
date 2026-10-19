import asyncio
from uuid import uuid4

import pytest
from app.contracts.jobs import AttemptLeaseV2, EnqueueJobV2
from app.domain.errors import DuplicateConflict, StaleAttempt
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import analysis_runs
from app.services.jobs import JobService
from scripts.phase1_fixture import scenario
from sqlalchemy import insert, select, text


async def job_scenario(factory):
    _, ctx, snapshot, _, _ = await scenario(factory)
    async with factory() as s, s.begin():
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=snapshot.repository_id,
                principal_id=ctx.principal_id,
                permission="write",
            )
        )
        await s.execute(insert(PHASE2_TABLES["org_work_limits"]).values(org_id=ctx.org_id))
    service = JobService(factory)
    request = EnqueueJobV2(
        repository_id=snapshot.repository_id,
        snapshot_id=snapshot.id,
        operation="inventory_snapshot",
        request_key=uuid4().hex,
        config_hash="a" * 64,
    )
    job = await service.enqueue(ctx, request)
    return service, ctx, snapshot, request, job


async def test_enqueue_replay_claim_race_and_attempt_bound_run(factory):
    service, ctx, snapshot, request, job = await job_scenario(factory)
    assert (await service.enqueue(ctx, request)).id == job.id
    with pytest.raises(DuplicateConflict):
        await service.enqueue(ctx, request.model_copy(update={"config_hash": "b" * 64}))
    claims = await asyncio.gather(
        service.claim(ctx.org_id, job.id, 1, "one"), service.claim(ctx.org_id, job.id, 1, "two")
    )
    leases = [x for x in claims if isinstance(x, AttemptLeaseV2)]
    assert len(leases) == 1
    lease = leases[0]
    assert lease.run_ref and lease.run_ref.snapshot_id == snapshot.id
    async with factory() as s:
        run = (
            (
                await s.execute(
                    select(analysis_runs).where(analysis_runs.c.id == lease.run_ref.run_id)
                )
            )
            .mappings()
            .one()
        )
        assert run["worker_job_id"] == job.id and run["worker_attempt_id"] == lease.attempt_id
        assert run["status"] == "running"
    renewed = await service.heartbeat(lease)
    assert renewed.lease_expires_at >= lease.lease_expires_at
    await service.cancel(ctx, job.id)
    with pytest.raises(StaleAttempt):
        await service.heartbeat(lease)
    assert (await service.get(ctx, job.id)).state == "cancelled"
    async with factory() as s:
        terminal = (
            await s.execute(
                select(analysis_runs.c.status, analysis_runs.c.failure_code).where(
                    analysis_runs.c.id == lease.run_ref.run_id
                )
            )
        ).one()
        assert terminal == ("cancelled", None)


async def test_retry_gets_fresh_run_and_old_callback_is_fenced(factory):
    service, ctx, _, _, job = await job_scenario(factory)
    first = await service.claim(ctx.org_id, job.id, 1, "one")
    assert isinstance(first, AttemptLeaseV2)
    await service.fail_attempt(first, "storage_unavailable", transient=True)
    async with factory() as s, s.begin():
        await s.execute(
            text("UPDATE jobs SET not_before=clock_timestamp() WHERE org_id=:org AND id=:job"),
            {"org": ctx.org_id, "job": job.id},
        )
    second = await service.claim(ctx.org_id, job.id, 2, "two")
    assert isinstance(second, AttemptLeaseV2)
    assert second.run_ref != first.run_ref and second.fence > first.fence
    with pytest.raises(StaleAttempt):
        await service.fail_attempt(first, "storage_unavailable", transient=True)
    async with factory() as s:
        assert (
            await s.scalar(
                select(analysis_runs.c.status).where(analysis_runs.c.id == first.run_ref.run_id)
            )
            == "failed"
        )
        assert (
            await s.scalar(
                select(analysis_runs.c.status).where(analysis_runs.c.id == second.run_ref.run_id)
            )
            == "running"
        )


async def test_deadline_sweep_needs_no_queue(factory):
    service, ctx, _, _, job = await job_scenario(factory)
    async with factory() as s, s.begin():
        await s.execute(
            text(
                "UPDATE jobs SET deadline=clock_timestamp()-interval '1 millisecond' "
                "WHERE org_id=:org AND id=:job"
            ),
            {"org": ctx.org_id, "job": job.id},
        )
    assert await service.recover_due() == 1
    current = await service.get(ctx, job.id)
    assert current.state == "failed" and current.failure_code == "deadline_exceeded"
    assert await service.recover_due() == 0

from datetime import timedelta

from app.models.phase2_tables import PHASE2_TABLES
from app.services.jobs import JobService
from app.workers.recovery import RecoveryWorker
from sqlalchemy import func, select, update

from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario


async def test_stranded_generation_and_exhaustion_diagnostic(factory, tmp_path):
    service, ctx, _, sub, body, _ = await submission_scenario(factory, tmp_path)
    uploaded = await service.write_content(ctx, sub.id, chunks(body))
    jobs, outbox = (PHASE2_TABLES[n] for n in ["jobs", "outbox"])
    async with factory.begin() as s:
        await s.execute(
            update(outbox)
            .where(outbox.c.job_id == uploaded.job_id)
            .values(state="sent", sent_at=func.clock_timestamp() - timedelta(seconds=181))
        )
    await RecoveryWorker(factory).tick()
    async with factory() as s:
        assert (
            await s.scalar(select(jobs.c.dispatch_generation).where(jobs.c.id == uploaded.job_id))
            == 2
        )
    lifecycle = JobService(factory)
    for generation in range(2, 5):
        lease = await lifecycle.claim(ctx.org_id, uploaded.job_id, generation, "fixture")
        await lifecycle.fail_attempt(lease, "storage_unavailable", transient=True)
        async with factory.begin() as s:
            await s.execute(
                update(jobs)
                .where(jobs.c.id == uploaded.job_id, jobs.c.state == "retry_wait")
                .values(not_before=func.clock_timestamp())
            )
    async with factory() as s:
        row = (await s.execute(select(jobs).where(jobs.c.id == uploaded.job_id))).mappings().one()
        assert row["state"] == "failed" and row["attempt_count"] == 3
        diagnostic = (
            (
                await s.execute(
                    select(outbox).where(
                        outbox.c.job_id == uploaded.job_id, outbox.c.kind == "dead_letter"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert (
            len(diagnostic) == 1 and diagnostic[0]["payload"]["error_code"] == "storage_unavailable"
        )
        assert (
            await s.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
                )
            )
            == 0
        )


async def test_authority_sweep_progresses_past_healthy_first_batch(factory, tmp_path):
    from uuid import UUID

    from app.contracts.persistence import RepositoryCreate
    from app.repositories.repository_repository import RepositoryRepository
    from sqlalchemy import insert

    _, ctx, request, _, _, _ = await submission_scenario(factory, tmp_path)
    async with factory.begin() as s:
        await s.execute(
            update(PHASE2_TABLES["org_work_limits"])
            .where(PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id)
            .values(max_pending_jobs=200)
        )
        other = await RepositoryRepository(s).create(
            ctx,
            RepositoryCreate(
                repository_key="revoked", display_name="Revoked", source_kind="archive"
            ),
        )
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=other.id,
                principal_id=ctx.principal_id,
                permission="write",
                revoked_at=func.clock_timestamp(),
            )
        )
        for index in range(1, 111):
            await s.execute(
                insert(PHASE2_TABLES["jobs"]).values(
                    org_id=ctx.org_id,
                    id=UUID(int=index),
                    repository_id=request.repository_id,
                    principal_id=ctx.principal_id,
                    operation="admit_source",
                    request_key=f"healthy-{index}",
                    request_hash="a" * 64,
                    config_hash="b" * 64,
                    deadline=func.clock_timestamp() + timedelta(minutes=15),
                )
            )
        target = UUID(int=(1 << 128) - 1)
        await s.execute(
            insert(PHASE2_TABLES["jobs"]).values(
                org_id=ctx.org_id,
                id=target,
                repository_id=other.id,
                principal_id=ctx.principal_id,
                operation="admit_source",
                request_key="revoked-target",
                request_hash="a" * 64,
                config_hash="b" * 64,
                deadline=func.clock_timestamp() + timedelta(minutes=15),
            )
        )
    worker = RecoveryWorker(factory)
    await worker.tick()
    await worker.tick()
    async with factory() as s:
        assert (
            await s.scalar(
                select(PHASE2_TABLES["jobs"].c.state).where(PHASE2_TABLES["jobs"].c.id == target)
            )
            == "cancelled"
        )

from app.models.tables import analysis_runs
from app.services.jobs import JobService
from app.workers.recovery import RecoveryWorker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.phase2.integration.test_job_lifecycle import job_scenario


async def test_api_cancel_fences_inventory_then_broker_closes_run(factory, db_values):
    service, ctx, _, _, job = await job_scenario(factory)
    lease = await service.claim(ctx.org_id, job.id, 1, "inventory")
    base = factory.kw["bind"].url
    api_engine = create_async_engine(
        base.set(username="p2_api", password=db_values["CODESPY_TEST_APP_PASSWORD"])
    )
    try:
        api = JobService(async_sessionmaker(api_engine, expire_on_commit=False))
        requested = await api.request_cancel(ctx, job.id)
        assert requested.state == "cancelling"
        await RecoveryWorker(factory).tick()
        assert (await service.get(ctx, job.id)).state == "cancelled"
        async with factory() as s:
            assert (
                await s.scalar(
                    select(analysis_runs.c.status).where(analysis_runs.c.id == lease.run_ref.run_id)
                )
                == "cancelled"
            )
    finally:
        await api_engine.dispose()

from app.models.phase2_tables import PHASE2_TABLES
from app.workers.recovery import RecoveryWorker
from sqlalchemy import func, select, update

from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario


async def test_revoked_authority_is_cancelled_without_queue(factory, tmp_path):
    service, ctx, _, sub, body, _ = await submission_scenario(factory, tmp_path)
    uploaded = await service.write_content(ctx, sub.id, chunks(body))
    async with factory.begin() as s:
        grants = PHASE2_TABLES["repository_grants"]
        await s.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id)
            .values(revoked_at=func.clock_timestamp())
        )
    await RecoveryWorker(factory).tick()
    async with factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(
                        PHASE2_TABLES["jobs"].c.id == uploaded.job_id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert job["state"] == "cancelled" and job["failure_code"] == "access_revoked"

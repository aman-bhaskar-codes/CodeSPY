"""Historical findings remain immutable when current source support disappears."""

import importlib

import pytest
from app.domain.errors import ScopeViolation
from app.domain.scope import RunRef
from app.models.phase2_tables import PHASE2_TABLES
from app.services.scan_publication import FindingFinalizer
from sqlalchemy import update
from tests.phase4.storage.test_scan_jobs import Registry
from tests.phase4.storage.test_scan_publication import publication_case


async def test_read_rechecks_source_without_mutating_historical_decisions(
    api_factory, broker_factory, migration_factory, tmp_path
):
    assert importlib.util.find_spec("app.services.scan_reads") is not None, "scan reads missing"
    ctx, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    service = importlib.import_module("app.services.scan_reads").ScanReadService(
        api_factory, Registry()
    )
    result = await service.read(ctx, lease.run_ref)
    assert result.historical == draft
    assert result.effective_decisions[0].effective_disposition == "supported-static"
    objects = PHASE2_TABLES["object_records"]
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(objects).where(objects.c.org_id == ctx.org_id).values(state="deleted")
        )
    result = await service.read(ctx, lease.run_ref)
    assert result.historical == draft
    assert result.effective_decisions[0].effective_disposition == "unresolved"
    assert result.effective_decisions[0].reason == "source_unavailable"
    assert draft.decisions[0].disposition == "supported-static"
    foreign = RunRef(
        org_id=ctx.principal_id, snapshot_id=lease.run_ref.snapshot_id, run_id=lease.run_ref.run_id
    )
    with pytest.raises(ScopeViolation):
        await service.read(ctx, foreign)


async def test_current_permission_is_required_for_historical_result(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from sqlalchemy import func

    ctx, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(ScopeViolation):
        await (
            importlib.import_module("app.services.scan_reads")
            .ScanReadService(api_factory, Registry())
            .read(ctx, lease.run_ref)
        )

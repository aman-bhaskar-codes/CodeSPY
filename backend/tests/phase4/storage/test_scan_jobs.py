"""API creates scan configs; broker claims distinct, immutable attempt runs."""

import importlib

import pytest
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.scan_jobs import NativeSelectionV1, ScanConfigV1
from app.domain.errors import DuplicateConflict
from app.models.scan_tables import scan_attempt_specs, scan_jobs, scan_specs
from app.scanners.finding_catalog import reviewed_catalog
from app.services.analysis_sources import locate_source
from app.services.jobs import JobService
from sqlalchemy import func, select
from tests.phase3.integration.test_structural_publication import admitted
from tests.phase4.runtime_fixtures import runtime


class Registry:
    """Unit composition double; these job tests do not qualify native execution."""

    def validate(self, config):
        if config.runtime_digest != "a" * 64:
            raise ValueError("unregistered test runtime")


async def scenario(migration_factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    async with migration_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
    from app.contracts.scanners import ScannerFileV1

    files = tuple(
        ScannerFileV1(file_id=f.file_id, sha256=f.sha256, size_bytes=f.size_bytes)
        for f in located.files
        if f.eligible and f.language == "python"
    )
    profile = runtime("semgrep").profile()
    config = ScanConfigV1(
        runtime_digest="a" * 64,
        catalog_digest=reviewed_catalog((profile,)).semantic_hash(),
        native_selections=(NativeSelectionV1(profile=profile, files=files),),
    )
    return ctx, snapshot, repo, store, config


async def test_api_create_replays_one_spec_and_broker_claim_binds_fresh_run(
    api_factory, broker_factory, migration_factory, tmp_path
):
    assert importlib.util.find_spec("app.services.scan_jobs") is not None, "scan service missing"
    service = importlib.import_module("app.services.scan_jobs").ScanJobService(
        api_factory, Registry()
    )
    ctx, snapshot, repo, _, config = await scenario(migration_factory, tmp_path)
    first = await service.create(ctx, repo, snapshot, config, "scan-request")
    second = await service.create(ctx, repo, snapshot, config, "scan-request")
    assert first == second
    async with broker_factory() as session:
        assert await session.scalar(select(func.count()).select_from(scan_specs)) == 1
    lease = await JobService(broker_factory).claim(ctx.org_id, first[1].id, 1, "scan-worker")
    assert isinstance(lease, AttemptLeaseV2) and lease.run_ref is not None
    async with broker_factory() as session:
        row = (await session.execute(select(scan_attempt_specs))).mappings().one()
        assert row["run_id"] == lease.run_ref.run_id
        assert row["config_hash"] == config.semantic_hash()
        assert row["publication_spec_json"]["scope"]["run_id"] == str(lease.run_ref.run_id)
    assert (
        await JobService(broker_factory).claim(ctx.org_id, first[1].id, 1, "other")
        == "already_leased"
    )


async def test_changed_request_and_unknown_runtime_do_not_create_work(
    api_factory, migration_factory, tmp_path
):
    assert importlib.util.find_spec("app.services.scan_jobs") is not None, "scan service missing"
    service = importlib.import_module("app.services.scan_jobs").ScanJobService(
        api_factory, Registry()
    )
    ctx, snapshot, repo, _, config = await scenario(migration_factory, tmp_path)
    await service.create(ctx, repo, snapshot, config, "scan-request")
    changed = config.model_copy(update={"catalog_digest": "f" * 64})
    with pytest.raises(DuplicateConflict):
        await service.create(ctx, repo, snapshot, changed, "scan-request")
    with pytest.raises(ValueError):
        await service.create(
            ctx, repo, snapshot, config.model_copy(update={"runtime_digest": "f" * 64}), "other"
        )
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(scan_jobs)) >= 1


async def test_cancelled_scan_retry_allocates_new_job_and_preserves_original_spec(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.services.retries import RetryService
    from app.services.scan_jobs import ScanJobService

    ctx, snapshot, repo, store, config = await scenario(migration_factory, tmp_path)
    spec, job = await ScanJobService(api_factory, Registry()).create(
        ctx, repo, snapshot, config, "scan"
    )
    await JobService(broker_factory).request_cancel(ctx, job.id)
    retries = RetryService(broker_factory, store)
    assert hasattr(retries, "scan_capabilities"), "scan retry integration missing"
    retries.scan_capabilities = Registry()
    first = await retries.retry(ctx, job.id, "scan-retry")
    second = await retries.retry(ctx, job.id, "scan-retry")
    assert first == second
    assert first.retry_job_id != job.id and first.scan_spec_id == spec.id
    assert (await JobService(broker_factory).get(ctx, job.id)).state == "cancelled"
    lease = await JobService(broker_factory).claim(
        ctx.org_id, first.retry_job_id, 1, "retry-worker"
    )
    assert isinstance(lease, AttemptLeaseV2) and lease.run_ref is not None


async def test_parallel_create_and_other_writer_retry_preserve_principal_and_reservations(
    api_factory, broker_factory, migration_factory, tmp_path
):
    import asyncio
    from uuid import uuid4

    from app.domain.errors import RetryRejected
    from app.domain.scope import TenantContext
    from app.models.phase2_tables import PHASE2_TABLES
    from app.models.tables import memberships, principals
    from app.services.retries import RetryService
    from app.services.scan_jobs import ScanJobService
    from sqlalchemy import insert

    ctx, snapshot, repo, store, config = await scenario(migration_factory, tmp_path)
    service = ScanJobService(api_factory, Registry())
    results = await asyncio.gather(
        *(service.create(ctx, repo, snapshot, config, "parallel") for _ in range(2))
    )
    assert results[0] == results[1]
    other = uuid4()
    async with migration_factory() as session, session.begin():
        await session.execute(
            insert(principals).values(id=other, subject_key=str(other), display_name="Other writer")
        )
        await session.execute(
            insert(memberships).values(org_id=ctx.org_id, principal_id=other, role="member")
        )
        await session.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id, repository_id=repo, principal_id=other, permission="write"
            )
        )
    writer = TenantContext(org_id=ctx.org_id, principal_id=other, role=ctx.role)
    with pytest.raises(DuplicateConflict):
        await service.create(writer, repo, snapshot, config, "parallel")
    _, distinct = await service.create(writer, repo, snapshot, config, "other-writer")
    assert distinct.id != results[0][1].id
    await JobService(broker_factory).request_cancel(ctx, results[0][1].id)
    retries = RetryService(broker_factory, store, scan_capabilities=Registry())
    with pytest.raises(RetryRejected):
        await retries.retry(writer, results[0][1].id, "foreign-retry")
    limits = PHASE2_TABLES["org_work_limits"]
    async with broker_factory() as session:
        assert (
            await session.scalar(
                select(limits.c.reserved_bytes).where(limits.c.org_id == ctx.org_id)
            )
            == 0
        )


async def test_overdue_queued_scan_is_terminalized_without_dispatcher_or_queue(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.services.scan_jobs import ScanJobService
    from sqlalchemy import text

    ctx, snapshot, repo, _, config = await scenario(migration_factory, tmp_path)
    _, job = await ScanJobService(api_factory, Registry()).create(
        ctx, repo, snapshot, config, "overdue"
    )
    async with migration_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE jobs SET deadline=clock_timestamp()-interval '1 second' "
                "WHERE org_id=:org AND id=:id"
            ),
            {"org": ctx.org_id, "id": job.id},
        )
    assert await JobService(broker_factory).recover_due() >= 1
    result = await JobService(broker_factory).get(ctx, job.id)
    assert result.state == "failed" and result.failure_code == "deadline_exceeded"
    assert result.result_run_id is None

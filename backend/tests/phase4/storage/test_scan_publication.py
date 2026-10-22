"""Real SQL publication with explicitly synthetic trusted scanner receipts."""

import importlib

from app.contracts.jobs import AttemptLeaseV2
from app.contracts.scanners import ScannerObservationV1, ScannerResultV1
from app.domain.source_ranges import SourceRangeV1
from app.models.scan_tables import finding_occurrences, scan_reports
from app.models.tables import analysis_runs
from app.repositories.scan_repository import ScanRepository
from app.scanners.finding_catalog import reviewed_catalog
from app.scanners.receipts import _issue
from app.services.findings import FindingMaterializer
from app.services.jobs import JobService
from app.services.scan_jobs import ScanJobService
from sqlalchemy import func, select
from tests.phase4.storage.test_scan_jobs import Registry, scenario


async def publication_case(api_factory, broker_factory, migration_factory, tmp_path, *, two=False):
    ctx, snapshot, repo, _store, config = await scenario(migration_factory, tmp_path)
    spec, job = await ScanJobService(api_factory, Registry()).create(
        ctx, repo, snapshot, config, "scan"
    )
    lease = await JobService(broker_factory).claim(ctx.org_id, job.id, 1, "publisher")
    assert isinstance(lease, AttemptLeaseV2)
    async with broker_factory() as session:
        publication = await ScanRepository(session).get_attempt_spec(lease)
    request = publication.native_requests[0]
    file = request.files[0]
    result = ScannerResultV1(
        request_digest=request.semantic_hash(),
        status="completed",
        processed_file_ids=tuple(f.file_id for f in request.files),
        omitted_file_ids=(),
        observations=tuple(
            ScannerObservationV1(
                rule_id="python.shell-true",
                file_id=file.file_id,
                sha256=file.sha256,
                range=SourceRangeV1(
                    start_byte=i,
                    end_byte=i + 1,
                    start_line=1,
                    end_line=1,
                    start_col=i,
                    end_col=i + 1,
                ),
            )
            for i in range(2 if two else 1)
        ),
    )
    draft = FindingMaterializer(reviewed_catalog((request.profile,))).materialize(
        publication, (_issue(request, result, "sha256:" + config.runtime_digest, "1.179.0"),)
    )
    return ctx, spec, job, lease, draft


async def test_scan_publication_is_atomic_and_exact_replay_is_idempotent(
    api_factory, broker_factory, migration_factory, tmp_path
):
    assert importlib.util.find_spec("app.services.scan_publication") is not None, (
        "scan finalizer missing"
    )
    case = await publication_case(api_factory, broker_factory, migration_factory, tmp_path)
    ctx, _, job, lease, draft = case
    finalizer = importlib.import_module("app.services.scan_publication").FindingFinalizer(
        broker_factory
    )
    result = await finalizer.finish(lease, draft)
    assert result == draft
    assert await finalizer.finish(lease, draft) == result
    public = await JobService(broker_factory).get(ctx, job.id)
    assert public.state == "succeeded" and public.report_available
    async with broker_factory() as session:
        assert await session.scalar(select(func.count()).select_from(scan_reports)) == 1
        assert await session.scalar(select(func.count()).select_from(finding_occurrences)) == 1
        assert (
            await session.scalar(
                select(analysis_runs.c.status).where(analysis_runs.c.id == lease.run_ref.run_id)
            )
            == "completed"
        )

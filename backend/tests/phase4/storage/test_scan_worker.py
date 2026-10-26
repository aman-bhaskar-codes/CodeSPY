"""Real job/database integration with explicitly synthetic native transport."""

import importlib

from app.contracts.scanners import ScannerResultV1
from app.scanners.finding_catalog import reviewed_catalog
from app.scanners.receipts import _issue
from app.services.jobs import JobService
from app.services.scan_jobs import ScanJobService
from app.services.scan_publication import FindingFinalizer
from app.services.scan_reads import ScanReadService
from sqlalchemy import text

from tests.phase4.storage.test_scan_jobs import Registry, scenario


async def test_scan_worker_releases_locks_before_transport_and_publishes_zero_matches(
    api_factory, broker_factory, migration_factory, tmp_path
):
    assert importlib.util.find_spec("app.workers.operations.scan_snapshot") is not None, (
        "scan worker missing"
    )
    ctx, snapshot, repo, store, config = await scenario(migration_factory, tmp_path)
    calls = []

    class Capabilities(Registry):
        def catalog(self, digest):
            catalog = reviewed_catalog(tuple(s.profile for s in config.native_selections))
            assert catalog.semantic_hash() == digest
            return catalog

        def native(self, request, located):
            class Native:
                async def scan_attested(self, received):
                    assert received == request
                    async with broker_factory() as session, session.begin():
                        await session.execute(text("SET LOCAL lock_timeout='100ms'"))
                        await session.execute(
                            text("SELECT org_id FROM org_work_limits WHERE org_id=:oid FOR UPDATE"),
                            {"oid": ctx.org_id},
                        )
                    calls.append(request.profile.tool)
                    result = ScannerResultV1(
                        request_digest=request.semantic_hash(),
                        status="completed",
                        processed_file_ids=tuple(f.file_id for f in request.files),
                        omitted_file_ids=(),
                        observations=(),
                    )
                    return _issue(request, result, "sha256:" + config.runtime_digest, "1.179.0")

            return Native()

    capabilities = Capabilities()
    _, job = await ScanJobService(api_factory, capabilities).create(
        ctx, repo, snapshot, config, "scan-worker"
    )
    lease = await JobService(broker_factory).claim(ctx.org_id, job.id, 1, "worker")
    operation = importlib.import_module("app.workers.operations.scan_snapshot").ScanOperation(
        broker_factory, store, capabilities, FindingFinalizer(broker_factory)
    )

    async def enter():
        calls.append("finalize")
        await JobService(broker_factory).heartbeat(lease)

    result = await operation.execute(lease, enter_finalization=enter)
    assert result.coverage == "complete" and result.findings == ()
    assert calls == ["semgrep", "finalize"]
    read = await ScanReadService(api_factory, capabilities).read(ctx, lease.run_ref)
    assert read.historical == result and read.effective_decisions == ()


async def test_raw_scanner_dto_cannot_be_published_as_completed_execution(
    api_factory, broker_factory, migration_factory, tmp_path
):
    import pytest
    from app.scanners.process import NativeProcessError
    from app.workers.operations.scan_snapshot import ScanOperation

    ctx, snapshot, repo, store, config = await scenario(migration_factory, tmp_path)

    class RawRegistry(Registry):
        def native(self, request, located):
            class Raw:
                async def scan_attested(self, received):
                    return ScannerResultV1(
                        request_digest=received.semantic_hash(),
                        status="completed",
                        processed_file_ids=tuple(f.file_id for f in received.files),
                        omitted_file_ids=(),
                        observations=(),
                    )

            return Raw()

    _, job = await ScanJobService(api_factory, RawRegistry()).create(
        ctx, repo, snapshot, config, "raw"
    )
    lease = await JobService(broker_factory).claim(ctx.org_id, job.id, 1, "worker")
    with pytest.raises(NativeProcessError, match="scanner_output_invalid"):
        await ScanOperation(
            broker_factory, store, RawRegistry(), FindingFinalizer(broker_factory)
        ).execute(lease)
    assert (await JobService(broker_factory).get(ctx, job.id)).state == "running"

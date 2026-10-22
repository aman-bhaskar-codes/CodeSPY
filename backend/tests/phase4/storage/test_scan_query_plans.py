"""Bounded publication and scoped query plans on synthetic, persisted findings."""

import json

from app.contracts.scan_jobs import NativeSelectionV1, ScanConfigV1
from app.contracts.scanners import ScannerFileV1, ScannerObservationV1, ScannerResultV1
from app.domain.source_ranges import SourceRangeV1
from app.repositories.scan_repository import ScanRepository
from app.scanners.finding_catalog import reviewed_catalog
from app.scanners.receipts import _issue
from app.services.analysis_sources import locate_source
from app.services.findings import FindingMaterializer
from app.services.jobs import JobService
from app.services.scan_jobs import ScanJobService
from app.services.scan_publication import FindingFinalizer
from sqlalchemy import text
from tests.phase4.runtime_fixtures import runtime
from tests.phase4.storage.scan_fixtures import admit_contents
from tests.phase4.storage.test_scan_jobs import Registry


async def test_thousand_findings_publish_atomically_and_scoped_plans_return_bounded_rows(
    api_factory, broker_factory, migration_factory, tmp_path
):
    # Synthetic receipt qualifies SQL capacity only, not native tool capacity.
    ctx, repo, snapshot, _ = await admit_contents(
        migration_factory, tmp_path, {"source.py": b"#" + b"x" * 2000 + b"\n"}
    )
    async with broker_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
    file = located.files[0]
    profile = runtime("semgrep").profile()
    catalog = reviewed_catalog((profile,))
    config = ScanConfigV1(
        runtime_digest="a" * 64,
        catalog_digest=catalog.semantic_hash(),
        native_selections=(
            NativeSelectionV1(
                profile=profile,
                files=(
                    ScannerFileV1(
                        file_id=file.file_id, sha256=file.sha256, size_bytes=file.size_bytes
                    ),
                ),
            ),
        ),
    )
    _, job = await ScanJobService(api_factory, Registry()).create(
        ctx, repo, snapshot, config, "capacity"
    )
    lease = await JobService(broker_factory).claim(ctx.org_id, job.id, 1, "capacity")
    async with broker_factory() as session:
        spec = await ScanRepository(session).get_attempt_spec(lease)
    request = spec.native_requests[0]
    result = ScannerResultV1(
        request_digest=request.semantic_hash(),
        status="completed",
        processed_file_ids=(file.file_id,),
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
            for i in range(1000)
        ),
    )
    draft = FindingMaterializer(catalog).materialize(
        spec, (_issue(request, result, "sha256:" + config.runtime_digest, "1.179.0"),)
    )
    assert len(draft.findings) == len(draft.evidence) == 1000
    await FindingFinalizer(broker_factory).finish(lease, draft)
    async with api_factory() as session:
        restored = await ScanRepository(session).read(ctx, lease.run_ref)
        assert restored == draft
        params = {
            "org": ctx.org_id,
            "snapshot": snapshot,
            "run": lease.run_ref.run_id,
            "evidence": draft.evidence[0].id,
        }
        plans = []
        for query, expected in (
            (
                "SELECT run_id FROM scan_reports WHERE org_id=:org "
                "AND snapshot_id=:snapshot AND run_id=:run",
                1,
            ),
            (
                "SELECT id FROM finding_occurrences WHERE org_id=:org AND snapshot_id=:snapshot "
                "AND run_id=:run ORDER BY id LIMIT 100",
                100,
            ),
            (
                "SELECT finding_id FROM finding_support WHERE org_id=:org "
                "AND snapshot_id=:snapshot "
                "AND run_id=:run AND evidence_id=:evidence LIMIT 100",
                1,
            ),
        ):
            plan = await session.scalar(
                text("EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON) " + query), params
            )
            plan = json.loads(plan) if isinstance(plan, str) else plan
            assert plan[0]["Plan"]["Actual Rows"] == expected
            plans.append(plan)
        (tmp_path / "scoped-plans.json").write_text(json.dumps(plans, indent=2))

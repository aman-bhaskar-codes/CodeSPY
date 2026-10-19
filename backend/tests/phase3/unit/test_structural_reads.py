from datetime import UTC, datetime
from uuid import uuid4

from app.analyzers.pipeline import analyze
from app.domain.canonical_json import canonical_bytes
from app.domain.scope import RunRef
from app.services.structural_reads import report_items, summary
from app.services.structural_results import materialize_result
from tests.phase3.unit.test_materialization import fixture_input


def test_summary_is_bounded_and_sections_preserve_complete_report(tmp_path):
    source = fixture_input(tmp_path)
    report = materialize_result(
        RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4()),
        analyze(source, tmp_path),
        source,
        tmp_path,
        datetime.now(UTC),
    ).report
    small = summary(report)
    assert not small.symbols and small.section_counts["symbols"] == len(report.symbols)
    assert len(canonical_bytes(small.model_dump())) < 65536
    assert report_items(report, "symbols") == report.symbols

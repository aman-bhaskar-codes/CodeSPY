from datetime import UTC, datetime
from uuid import uuid4

from app.analyzers.pipeline import analyze
from app.contracts.structure import StructuralLimitsV1
from app.domain.scope import RunRef
from app.services.structural_results import materialize_result
from tests.phase3.unit.test_materialization import fixture_input


def test_output_capacity_is_an_honest_file_omission(tmp_path):
    source = fixture_input(tmp_path)
    config = source.config.model_copy(update={"limits": StructuralLimitsV1(nodes=1)})
    source = source.model_copy(update={"config": config, "config_hash": config.semantic_hash()})
    result = analyze(source, tmp_path)
    assert result.coverage.omitted_files == 1
    assert result.coverage.syntax == "partial"
    assert result.coverage.processed_files == 0
    assert not result.symbols
    stored = materialize_result(
        RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4()),
        result,
        source,
        tmp_path,
        datetime.now(UTC),
    )
    assert stored.batch.disposition == "partial"

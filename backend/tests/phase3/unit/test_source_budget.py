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


def test_dense_long_symbols_yield_partial_instead_of_oversized_report(tmp_path):
    import hashlib
    import io
    import zipfile

    from app.admission.zip_inventory import inspect_zip_bytes
    from app.contracts.ingestion import AdmissionPolicyV1
    from app.contracts.structure import SourceDescriptorV1, SourceFileV1

    source = fixture_input(tmp_path)
    data = b"".join(f"def f_{i:03}_{'x' * 1100}(): pass\n".encode() for i in range(800))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.py", data)
    raw = buffer.getvalue()
    manifest = inspect_zip_bytes(raw, AdmissionPolicyV1()).model_dump_json().encode()
    (tmp_path / "archive.zip").write_bytes(raw)
    (tmp_path / "manifest.json").write_bytes(manifest)
    source = source.model_copy(
        update={
            "archive": SourceDescriptorV1(
                path="archive.zip", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
            ),
            "manifest": SourceDescriptorV1(
                path="manifest.json",
                sha256=hashlib.sha256(manifest).hexdigest(),
                size_bytes=len(manifest),
            ),
            "files": (
                SourceFileV1(
                    file_id=uuid4(),
                    path="main.py",
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    eligible=True,
                    language="python",
                ),
            ),
        }
    )
    draft = analyze(source, tmp_path)
    result = materialize_result(
        RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4()),
        draft,
        source,
        tmp_path,
        datetime.now(UTC),
    )
    assert result.batch.disposition == "partial" and result.report.coverage.omitted_files == 1


def test_omission_detail_bytes_are_bounded_without_changing_exact_counts(tmp_path):
    from app.analyzers.capacity import bound_draft
    from app.analyzers.source import AdmittedSourceReader
    from app.contracts.structure import FileOmissionV1, SourceFileV1

    source = fixture_input(tmp_path)
    units = AdmittedSourceReader(source, tmp_path).read_selected()
    draft = analyze(source, tmp_path)
    omitted = tuple(
        SourceFileV1(
            file_id=uuid4(),
            path=f"omitted_{i}/" + "x" * 900 + ".py",
            sha256="a" * 64,
            size_bytes=1,
            eligible=True,
            language="python",
        )
        for i in range(3000)
    )
    # Synthetic sizing DTOs: source acquisition/authority are covered separately.
    source = source.model_copy(update={"files": (*source.files, *omitted)})
    coverage = draft.coverage.model_copy(
        update={
            "requested_files": 3001,
            "omitted_files": 3000,
            "inventoried_files": len(source.files),
            "eligible_files": len(source.files),
            "omissions": tuple(
                FileOmissionV1(file_id=f.file_id, path=f.path, reason="source_capacity")
                for f in omitted
            ),
        }
    )
    bounded = bound_draft(draft.model_copy(update={"coverage": coverage}), source, units)
    assert bounded.coverage.omitted_files == 3000
    assert len(bounded.coverage.omissions) + bounded.coverage.omitted_details_count == 3000
    assert bounded.coverage.omitted_details_count > 0

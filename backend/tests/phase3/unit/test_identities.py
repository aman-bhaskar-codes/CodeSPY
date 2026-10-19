from uuid import uuid4

from app.analyzers.identities import normalize_report, observation_key
from app.contracts.structure import (
    ArchitectureReportV1,
    SourceLocationV1,
    StructuralCoverageV1,
    SymbolObservationV1,
)
from app.domain.scope import RunRef
from app.domain.source_ranges import SourceRangeV1


def test_observation_key_deterministic():
    file_id = uuid4()
    loc = SourceLocationV1(
        file_id=file_id,
        sha256="0" * 64,
        range=SourceRangeV1(
            start_byte=0, end_byte=10, start_line=1, end_line=1, start_col=0, end_col=10
        ),
    )
    obs1 = SymbolObservationV1(
        semantic_key="1" * 64,
        kind="function",
        path="main.py",
        sha256="0" * 64,
        location=loc,
        name="hello",
        qualified_name="main.hello",
        role="declaration",
    )
    obs2 = SymbolObservationV1(
        semantic_key="2" * 64,
        kind="function",
        path="main.py",
        sha256="0" * 64,
        location=loc,
        name="hello",
        qualified_name="main.hello",
        role="declaration",
    )
    assert observation_key(obs1) == observation_key(obs2)


def test_normalize_report_strips_physical_identities():
    file_id = uuid4()
    loc = SourceLocationV1(
        file_id=file_id,
        sha256="0" * 64,
        range=SourceRangeV1(
            start_byte=0, end_byte=10, start_line=1, end_line=1, start_col=0, end_col=10
        ),
    )
    sym = SymbolObservationV1(
        semantic_key="1" * 64,
        kind="function",
        path="main.py",
        sha256="0" * 64,
        location=loc,
        name="hello",
        qualified_name="main.hello",
        role="declaration",
    )
    coverage = StructuralCoverageV1()
    run_ref = RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())
    report = ArchitectureReportV1(
        request_nonce="nonce-1",
        config_hash="a" * 64,
        toolchain_hash="b" * 64,
        archive_sha256="c" * 64,
        policy_hash="d" * 64,
        run_ref=run_ref,
        symbols=(sym,),
        coverage=coverage,
    )
    normalized = normalize_report(report)
    assert "run_ref" not in normalized
    assert "request_nonce" not in normalized
    assert "symbol_map" not in normalized
    assert "evidence_map" not in normalized
    # Check that file_id was mapped to source: path and sha256
    symbol_dict = normalized["symbols"][0]
    assert "location" in symbol_dict
    assert symbol_dict["location"]["source"]["path"] == "main.py"
    assert symbol_dict["location"]["source"]["sha256"] == "0" * 64

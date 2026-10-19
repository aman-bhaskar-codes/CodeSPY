from uuid import uuid4

import pytest
from app.contracts.structure import ResolutionV1, StructuralAnalysisRequestV1, StructuralCoverageV1
from pydantic import ValidationError


def test_prefix_normalization_and_rejection():
    request = StructuralAnalysisRequestV1(
        snapshot_id=uuid4(), include_prefixes=("src/", "lib", "lib")
    )
    assert request.include_prefixes == ("lib", "src")
    with pytest.raises(ValidationError):
        StructuralAnalysisRequestV1(snapshot_id=uuid4(), include_prefixes=("../src",))


def test_resolution_cannot_assert_partial_candidate_set():
    with pytest.raises(ValidationError):
        ResolutionV1(
            site_key="a" * 64, resolution_kind="binding", status="resolved", candidate_keys=()
        )


def test_coverage_reconciles():
    with pytest.raises(ValidationError):
        StructuralCoverageV1(requested_files=2, processed_files=1)


def test_source_input_rejects_foreign_config_and_duplicates():
    from app.contracts.ingestion import AdmissionPolicyV1
    from app.contracts.structure import AnalysisConfigV1, SourceInputV1

    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    policy = AdmissionPolicyV1()
    descriptor = {"path": "archive.zip", "sha256": "a" * 64, "size_bytes": 1}
    document = {
        "archive": descriptor,
        "manifest": descriptor,
        "files": (),
        "policy": {"admission": policy, "policy_hash": policy.digest()},
        "config": config,
        "config_hash": config.semantic_hash(),
        "toolchain_hash": config.toolchain_hash,
        "request_nonce": "attempt-1",
    }
    assert SourceInputV1.model_validate(document).config == config
    with pytest.raises(ValidationError):
        SourceInputV1.model_validate({**document, "config_hash": "b" * 64})
    entry = {
        "file_id": uuid4(),
        "path": "app.py",
        "sha256": "a" * 64,
        "size_bytes": 1,
        "eligible": True,
    }
    with pytest.raises(ValidationError):
        SourceInputV1.model_validate({**document, "files": (entry, entry)})


def test_draft_cannot_claim_wrong_excerpt_digest():
    from app.contracts.structure import SourceExcerptV1
    from app.domain.source_ranges import SourceRangeV1

    with pytest.raises(ValidationError):
        SourceExcerptV1(
            location={
                "file_id": uuid4(),
                "sha256": "a" * 64,
                "range": SourceRangeV1(
                    start_byte=0, end_byte=1, start_line=1, end_line=1, start_col=0, end_col=1
                ),
            },
            text="x",
            sha256="a" * 64,
        )

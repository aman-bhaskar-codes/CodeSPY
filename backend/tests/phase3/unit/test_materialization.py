import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.analyzers.pipeline import analyze
from app.contracts.ingestion import AdmissionPolicyV1, ArtifactManifestV1
from app.contracts.structure import (
    AnalysisConfigV1,
    SourceDescriptorV1,
    SourceFileV1,
    SourceInputV1,
    SourceLocationV1,
    SourcePolicyV1,
)
from app.domain.scope import RunRef
from app.services.structural_results import materialize_result

from tests.phase3.unit.test_source import create_test_archive


def fixture_input(root):
    archive, manifest, _ = create_test_archive()
    (root / "archive.zip").write_bytes(archive)
    (root / "manifest.json").write_bytes(manifest)
    policy = AdmissionPolicyV1()
    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    entries = ArtifactManifestV1.model_validate_json(manifest).entries
    return SourceInputV1(
        archive=SourceDescriptorV1(
            path="archive.zip", sha256=hashlib.sha256(archive).hexdigest(), size_bytes=len(archive)
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(manifest).hexdigest(),
            size_bytes=len(manifest),
        ),
        files=tuple(
            SourceFileV1(
                file_id=uuid4(),
                path=e.path,
                sha256=e.sha256,
                size_bytes=e.size_bytes,
                language=e.language_hint,
                eligible=e.eligible,
                exclusion_reason=e.reason,
            )
            for e in entries
        ),
        policy=SourcePolicyV1(policy_hash=policy.digest(), admission=policy),
        config=config,
        config_hash=config.semantic_hash(),
        toolchain_hash=config.toolchain_hash,
        request_nonce="fixture",
    )


def test_materialization_publishes_nodes_evidence_and_containment(tmp_path):
    source = fixture_input(tmp_path)
    draft = analyze(source, tmp_path)
    ref = RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())
    result = materialize_result(ref, draft, source, tmp_path, datetime.now(UTC))
    assert len(result.batch.nodes) == len(draft.symbols)
    assert result.batch.evidence
    assert result.report.symbol_map
    assert all(
        e.support_evidence_id in {x.id for x in result.batch.evidence} for e in result.batch.edges
    )
    assert (
        materialize_result(ref, draft, source, tmp_path, result.batch.evidence[0].captured_at)
        == result
    )


def test_materialization_rejects_untrusted_ranges_and_nonce(tmp_path):
    source = fixture_input(tmp_path)
    draft = analyze(source, tmp_path)
    ref = RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())
    with pytest.raises(ValueError, match="identity"):
        materialize_result(
            ref,
            draft.model_copy(update={"request_nonce": "foreign"}),
            source,
            tmp_path,
            datetime.now(UTC),
        )
    symbol = draft.symbols[0]
    span = symbol.location.range.model_copy(update={"end_col": 999})
    altered = symbol.model_copy(
        update={
            "location": SourceLocationV1(file_id=symbol.file_id, sha256=symbol.sha256, range=span)
        }
    )
    with pytest.raises(ValueError, match="source location"):
        materialize_result(
            ref,
            draft.model_copy(update={"symbols": (altered,)}),
            source,
            tmp_path,
            datetime.now(UTC),
        )

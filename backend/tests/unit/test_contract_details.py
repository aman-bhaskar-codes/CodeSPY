import json
from datetime import datetime
from uuid import uuid4

import pytest
from app.contracts.evidence import EvidenceEnvelopeV1
from app.contracts.persistence import ResultBatchV1, SourceFileCreate
from app.domain.canonical_json import canonical_bytes
from app.domain.scope import RunRef
from app.domain.source_ranges import SourceRangeV1
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.fixtures import batch_for


@pytest.mark.parametrize("path", ["/root", "a//b", "a/../b", "./a", "a/", "a\\b", "a\x00b"])
def test_unsafe_paths(path):
    with pytest.raises(ValidationError):
        SourceFileCreate(id=uuid4(), path=path, sha256="a" * 64, size_bytes=0)


@given(
    st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), min_size=1, max_size=30
    )
)
def test_unicode_paths_preserved(path):
    file = SourceFileCreate(id=uuid4(), path=path + ".py", sha256="a" * 64, size_bytes=0)
    assert file.path == path + ".py"


def test_wire_uuid_and_evidence_digest():
    ref = RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())
    assert RunRef.model_validate_json(ref.model_dump_json()) == ref
    batch = batch_for(ref, [uuid4(), uuid4()])
    e = batch.evidence[0]
    assert e.semantic_hash() == e.model_copy(update={"id": uuid4()}).semantic_hash()
    assert e.semantic_hash() != e.model_copy(update={"payload": {"changed": True}}).semantic_hash()
    with pytest.raises(ValidationError):
        EvidenceEnvelopeV1.model_validate({**e.model_dump(), "captured_at": datetime(2026, 1, 1)})
    with pytest.raises(ValidationError):
        EvidenceEnvelopeV1.model_validate({**e.model_dump(), "file_id": None})
    assert b"Z" in canonical_bytes({"timestamp": e.captured_at})


def test_lineage_cycles_rejected():
    ref = RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())
    batch = batch_for(ref, [uuid4(), uuid4()])
    a, b = [e.id for e in batch.evidence]
    with pytest.raises(ValidationError):
        ResultBatchV1.model_validate(
            {
                **batch.model_dump(),
                "derivations": [{"parent_id": a, "child_id": b}, {"parent_id": b, "child_id": a}],
            }
        )


def test_range_byte_convention():
    source = "def café():\r\n    pass\r\n".encode()
    r = SourceRangeV1(
        start_byte=0,
        end_byte=source.index(b"\n") + 1,
        start_line=1,
        end_line=2,
        start_col=0,
        end_col=0,
    )
    assert source[r.start_byte : r.end_byte].endswith(b"\r\n")


def test_python_uuid_is_strict_but_json_is_portable():
    ref = RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())
    with pytest.raises(ValidationError):
        RunRef.model_validate(json.loads(ref.model_dump_json()))
    assert RunRef.model_validate_json(ref.model_dump_json()) == ref


def test_artifact_keys_are_opaque_not_urls():
    from app.contracts.persistence import ArtifactCreate

    with pytest.raises(ValidationError):
        ArtifactCreate(
            sha256="a" * 64,
            size_bytes=1,
            storage_key="https://example.test/object?credential=fixture",
        )


def test_demo_metadata_comes_from_known_utf8_sources():
    from hashlib import sha256

    from scripts.phase1_fixture import FIXTURE_SOURCES, fixture_files

    ids = [uuid4(), uuid4()]
    files = fixture_files(ids)
    assert b"\r\n" in FIXTURE_SOURCES["é.py"]
    for file in files:
        assert file.sha256 == sha256(FIXTURE_SOURCES[file.path]).hexdigest()
        assert file.size_bytes == len(FIXTURE_SOURCES[file.path])

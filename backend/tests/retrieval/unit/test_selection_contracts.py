"""Additive provenance decoding does not itself qualify a stored chunk set."""

from uuid import uuid4

from app.contracts.memory import EvidencePointerV1
from app.contracts.search import SearchScopeV1


def test_scope_preserves_explicit_set_identity_and_legacy_default():
    values = {
        "org_id": uuid4(),
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "principal_id": uuid4(),
    }
    identity = uuid4()
    assert SearchScopeV1(**values, chunk_set_id=identity).chunk_set_id == identity
    assert SearchScopeV1(**values).chunk_set_id is None


def test_evidence_pointer_preserves_materialization_and_safe_digest():
    identity = uuid4()
    pointer = EvidencePointerV1(
        file_path="a.py",
        start_line=1,
        end_line=1,
        chunk_id=uuid4(),
        chunk_set_id=identity,
        materialization_hash="a" * 64,
        safe_content_sha256="b" * 64,
    )
    restored = EvidencePointerV1.model_validate_json(pointer.model_dump_json())
    assert restored.chunk_set_id == identity
    assert restored.materialization_hash == "a" * 64
    assert restored.safe_content_sha256 == "b" * 64


def test_public_requests_accept_set_pin_without_fabricating_identity():
    from app.contracts.context import ContextAssemblyRequestV1
    from app.contracts.search import HybridSearchRequestV1

    identity = uuid4()
    for model in (ContextAssemblyRequestV1, HybridSearchRequestV1):
        request = model(query="print", chunk_set_id=identity)
        assert request.chunk_set_id == identity and request.scope is None


def test_public_set_pin_cannot_conflict_with_scoped_selection():
    import pytest
    from app.contracts.search import HybridSearchRequestV1
    from pydantic import ValidationError

    scope = SearchScopeV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        chunk_set_id=uuid4(),
    )
    with pytest.raises(ValidationError):
        HybridSearchRequestV1(scope=scope, query="print", chunk_set_id=uuid4())

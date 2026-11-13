"""Claim support must bind the exact delivered excerpt and its persisted identity."""

import hashlib
from uuid import uuid4

import pytest
from app.contracts.investigation_execution import (
    ClaimVerificationStatus,
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationEvidenceV1,
)
from app.services.investigation_verification import InvestigationVerificationService

from tests.investigations.unit.test_executor import _make_context, _make_spec


@pytest.mark.parametrize("tampered", [False, True])
async def test_delivered_quote_requires_matching_digest_scope_and_range(tampered):
    scope = _make_context(_make_spec()).scope
    payload = '20: value = "a  b"'
    evidence = InvestigationEvidenceV1(
        id=uuid4(),
        org_id=scope.org_id,
        investigation_id=scope.investigation_id,
        snapshot_id=scope.snapshot_id,
        source_path="a.py",
        file_sha256="a" * 64,
        original_start_line=20,
        original_end_line=20,
        safe_start_line=20,
        safe_end_line=20,
        excerpt_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        observation_type="source_read",
    )
    claim = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="Literal has two spaces",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        evidence_ids=(evidence.id,),
        suggested_file="a.py",
        suggested_line_start=20,
        suggested_line_end=20,
        suggested_quote='"a  b"',
    )
    outcome = await InvestigationVerificationService().verify_claims(
        scope,
        (claim,),
        (evidence,),
        delivered_excerpts={evidence.id: payload + ("tampered" if tampered else "")},
    )
    assert outcome.verifications[0].status == (
        ClaimVerificationStatus.UNSUPPORTED
        if tampered
        else ClaimVerificationStatus.SOURCE_CORRESPONDENCE_CONFIRMED
    )


async def test_unsupported_true_positive_cannot_bypass_grounding():
    scope = _make_context(_make_spec()).scope
    result = await InvestigationVerificationService().verify_claims(
        scope, (), original_verdict="true_positive"
    )
    assert result.adjusted_verdict == "indeterminate"


def test_report_fact_evidence_has_a_ref_and_no_source_fields():
    from uuid import uuid4

    from app.contracts.investigation_execution import InvestigationEvidenceV1
    from pydantic import ValidationError

    ids = {
        "id": uuid4(),
        "org_id": uuid4(),
        "investigation_id": uuid4(),
        "snapshot_id": uuid4(),
        "excerpt_sha256": "a" * 64,
    }
    fact = InvestigationEvidenceV1(
        **ids, observation_type="report_fact", fact_ref="d" * 8 + ":abcd"
    )
    assert fact.source_path is None and fact.original_start_line is None
    for bad in (
        {"observation_type": "report_fact"},  # no ref
        {"observation_type": "report_fact", "fact_ref": "x", "source_path": "a.py"},  # mixed
        {"observation_type": "source_read"},  # source row with no source
        {
            "observation_type": "source_read",
            "fact_ref": "x",
            "source_path": "a.py",
            "file_sha256": "b" * 64,
            "original_start_line": 1,
            "original_end_line": 2,
            "safe_start_line": 1,
            "safe_end_line": 2,
        },
    ):
        with pytest.raises(ValidationError):
            InvestigationEvidenceV1(**ids, **bad)

"""Tests for deterministic evidence verification and claim grounding (Task H).

Validates that claims cannot pass grounding vacuously, file existence does not prove
truth without quoted source, cross-snapshot citations are rejected, redacted secret quotes
are denied, and conservative verdict eligibility prevents trivial quotes from outvoting
unsupported security facts.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.contracts.investigation_execution import (
    ClaimVerificationStatus,
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationEvidenceV1,
    InvestigationScopeV1,
)
from app.services.investigation_verification import (
    InvestigationVerificationService,
)


def _make_scope(*, snapshot_id=None, target_type="finding") -> InvestigationScopeV1:
    return InvestigationScopeV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=snapshot_id or uuid4(),
        principal_id=uuid4(),
        investigation_id=uuid4(),
        spec_hash="a" * 64,
        execution_revision=1,
    )


@pytest.mark.asyncio
async def test_zero_claims_not_grounded():
    """Zero claims must NOT produce grounded=True by vacuous truth."""
    scope = _make_scope()
    service = InvestigationVerificationService()

    outcome = await service.verify_claims(
        scope=scope,
        claims=(),
        evidence_records=(),
        original_verdict="vulnerable",
        original_confidence=0.9,
    )

    assert outcome.is_grounded is False
    assert outcome.grounding_ratio == 0.0
    assert outcome.adjusted_verdict == "indeterminate"
    assert outcome.adjusted_confidence <= 0.4
    assert "No specific claims provided" in outcome.summary


@pytest.mark.asyncio
async def test_quote_free_file_existence_unsupported():
    """A claim citing a valid file/line range without a supporting quote is UNSUPPORTED."""
    scope = _make_scope()
    service = InvestigationVerificationService()

    file_content = "line 1\nline 2\nline 3\nline 4\nline 5\n"

    async def mock_resolver(path: str) -> str | None:
        if path == "src/app.py":
            return file_content
        return None

    claim = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="App exists and has lines 1-3",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        suggested_file="src/app.py",
        suggested_line_start=1,
        suggested_line_end=3,
        suggested_quote=None,  # No supporting quote
    )

    outcome = await service.verify_claims(
        scope=scope,
        claims=(claim,),
        evidence_records=(),
        file_resolver=mock_resolver,
        original_verdict="vulnerable",
        original_confidence=0.85,
    )

    assert len(outcome.verifications) == 1
    v = outcome.verifications[0]
    assert v.status == ClaimVerificationStatus.UNSUPPORTED
    assert "lacks supporting quote" in v.notes
    assert outcome.is_grounded is False
    assert outcome.adjusted_verdict == "indeterminate"


@pytest.mark.asyncio
async def test_cross_snapshot_citation_rejected():
    """Citations pointing to a different snapshot ID must be rejected."""
    snapshot_a = uuid4()
    snapshot_b = uuid4()
    scope = _make_scope(snapshot_id=snapshot_a)
    service = InvestigationVerificationService()

    evidence_b = InvestigationEvidenceV1(
        id=uuid4(),
        org_id=scope.org_id,
        investigation_id=scope.investigation_id,
        snapshot_id=snapshot_b,  # DIFFERENT snapshot!
        source_path="src/main.py",
        file_sha256="b" * 64,
        original_start_line=1,
        original_end_line=5,
        safe_start_line=1,
        safe_end_line=5,
        excerpt_sha256="c" * 64,
        observation_type="read_file_range",
    )

    claim = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="Code in other snapshot",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        evidence_ids=(evidence_b.id,),
        suggested_file="src/main.py",
        suggested_line_start=1,
        suggested_line_end=5,
        suggested_quote="some quote",
    )

    outcome = await service.verify_claims(
        scope=scope,
        claims=(claim,),
        evidence_records=(evidence_b,),
        original_verdict="vulnerable",
    )

    v = outcome.verifications[0]
    assert v.status == ClaimVerificationStatus.UNSUPPORTED
    assert "Cross-snapshot citation rejected" in v.notes
    assert outcome.is_grounded is False


@pytest.mark.asyncio
async def test_wrong_line_range_or_missing_quote():
    """Quotes in the wrong line range return LOCATION_VALID_QUOTE_MISSING.

    Missing quotes return REFUTED.
    """
    scope = _make_scope()
    service = InvestigationVerificationService()

    file_content = (
        "import os\n"
        "import sys\n"
        "# line 3\n"
        "# line 4\n"
        "secret_token = 'xyz123'\n"  # line 5
    )

    async def mock_resolver(path: str) -> str | None:
        if path == "config.py":
            return file_content
        return None

    # Claim 1: cites lines 1-2, but quote is on line 5
    claim_wrong_lines = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="Token assignment at lines 1-2",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        suggested_file="config.py",
        suggested_line_start=1,
        suggested_line_end=2,
        suggested_quote="secret_token = 'xyz123'",
    )

    # Claim 2: quote not in file at all
    claim_not_in_file = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="Nonexistent function call",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        suggested_file="config.py",
        suggested_line_start=1,
        suggested_line_end=5,
        suggested_quote="def totally_fabricated_function():",
    )

    outcome = await service.verify_claims(
        scope=scope,
        claims=(claim_wrong_lines, claim_not_in_file),
        evidence_records=(),
        file_resolver=mock_resolver,
        original_verdict="vulnerable",
    )

    v1 = outcome.verifications[0]
    assert v1.status == ClaimVerificationStatus.LOCATION_VALID_QUOTE_MISSING
    assert "not in cited line range" in v1.notes

    v2 = outcome.verifications[1]
    assert v2.status == ClaimVerificationStatus.REFUTED
    assert "not found" in v2.notes


@pytest.mark.asyncio
async def test_redacted_secret_quote_rejected():
    """Quotes attempting to match redacted placeholders or token masks are rejected."""
    scope = _make_scope()
    service = InvestigationVerificationService()

    file_content = "api_key = '***REDACTED***'\n"

    async def mock_resolver(path: str) -> str | None:
        return file_content

    claim = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="API key is exposed",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        suggested_file="keys.py",
        suggested_line_start=1,
        suggested_line_end=1,
        suggested_quote="api_key = '***REDACTED***'",
    )

    outcome = await service.verify_claims(
        scope=scope,
        claims=(claim,),
        evidence_records=(),
        file_resolver=mock_resolver,
        original_verdict="vulnerable",
    )

    v = outcome.verifications[0]
    assert v.status == ClaimVerificationStatus.UNSUPPORTED
    assert "redacted secret" in v.notes.lower()
    assert outcome.is_grounded is False


@pytest.mark.asyncio
async def test_conservative_verdict_eligibility_outvoting_prevention():
    """9 trivial supported quotes cannot outvote 1 unsupported security hypothesis."""
    scope = _make_scope()
    service = InvestigationVerificationService()

    lines = [f"const val{i} = {i};\n" for i in range(1, 11)]
    file_content = "".join(lines)

    async def mock_resolver(path: str) -> str | None:
        return file_content

    # 9 trivial source observations
    claims: list[InvestigationClaimV2] = []
    for i in range(1, 10):
        claims.append(
            InvestigationClaimV2(
                claim_id=uuid4(),
                statement=f"val{i} is declared",
                kind=InvestigationClaimKind.SOURCE_OBSERVATION,
                suggested_file="data.js",
                suggested_line_start=i,
                suggested_line_end=i,
                suggested_quote=f"const val{i} = {i};",
            )
        )

    # 1 critical security hypothesis that is unsupported
    claims.append(
        InvestigationClaimV2(
            claim_id=uuid4(),
            statement="User input reaches SQL query without parameterization",
            kind=InvestigationClaimKind.SECURITY_HYPOTHESIS,
            suggested_file="data.js",
            suggested_line_start=10,
            suggested_line_end=10,
            suggested_quote="SELECT * FROM users WHERE id = x",
        )
    )

    outcome = await service.verify_claims(
        scope=scope,
        claims=tuple(claims),
        evidence_records=(),
        file_resolver=mock_resolver,
        original_verdict="vulnerable",
        original_confidence=0.95,
    )

    # 9/10 = 90% grounding ratio, but 1 security hypothesis was refuted/unsupported!
    assert outcome.grounding_ratio == 0.9
    assert outcome.is_grounded is False
    assert outcome.adjusted_verdict == "indeterminate"
    assert outcome.adjusted_confidence <= 0.4
    assert "hypotheses unsupported or refuted" in outcome.summary


@pytest.mark.asyncio
async def test_partial_no_hit_benign_conclusion_rejected():
    """Partial or no-hit search cannot conclude whole-repo benign without verified proof."""
    scope = _make_scope(target_type="repository")
    service = InvestigationVerificationService()

    # Model proposes benign with no claims or partial coverage
    outcome = await service.verify_claims(
        scope=scope,
        claims=(),
        evidence_records=(),
        original_verdict="benign",
        original_confidence=0.8,
        coverage_notes="Searched 2 files, found nothing.",
    )

    assert outcome.adjusted_verdict == "indeterminate"
    assert outcome.adjusted_confidence <= 0.4

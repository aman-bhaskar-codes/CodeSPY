"""Integration tests for claim verification and report draft preparation (Task H)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.contracts.investigation_execution import (
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationReportDraftV1,
    InvestigationScopeV1,
    InvestigationStopReason,
)
from app.services.investigation_verification import (
    InvestigationVerificationService,
)


@pytest.mark.asyncio
async def test_prepare_report_draft_with_grounded_claims():
    """Valid grounded claims produce a valid InvestigationReportDraftV1 with matching digest."""
    scope = InvestigationScopeV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        investigation_id=uuid4(),
        spec_hash="f" * 64,
        execution_revision=1,
    )
    service = InvestigationVerificationService()

    file_content = "def execute_command(cmd):\n    os.system(cmd)\n"

    async def mock_resolver(path: str) -> str | None:
        if path == "vuln.py":
            return file_content
        return None

    claim = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="Command execution calls os.system directly",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        suggested_file="vuln.py",
        suggested_line_start=1,
        suggested_line_end=2,
        suggested_quote="os.system(cmd)",
    )

    outcome = await service.verify_claims(
        scope=scope,
        claims=(claim,),
        evidence_records=(),
        file_resolver=mock_resolver,
        original_verdict="vulnerable",
        original_confidence=0.9,
    )

    assert outcome.is_grounded is True
    assert outcome.adjusted_verdict == "vulnerable"
    assert outcome.adjusted_confidence == 0.9

    draft = service.prepare_draft(
        scope=scope,
        outcome=outcome,
        claims=(claim,),
        evidence_refs=(),
        coverage_notes="Full inspection complete",
        settled_usage_micros=120_000,
        stop_reason=InvestigationStopReason.COMPLETED,
    )

    assert isinstance(draft, InvestigationReportDraftV1)
    assert draft.investigation_id == scope.investigation_id
    assert draft.spec_hash == scope.spec_hash
    assert draft.verdict == "vulnerable"
    assert draft.verdict_confidence == 0.9
    assert draft.settled_usage_micros == 120_000
    assert draft.stop_reason == InvestigationStopReason.COMPLETED
    assert len(draft.claims) == 1
    assert len(draft.verifications) == 1
    assert draft.draft_digest == draft.compute_draft_digest()


@pytest.mark.asyncio
async def test_prepare_report_draft_bounds_exceeded():
    """Exceeding claims or evidence bounds raises validation error."""
    scope = InvestigationScopeV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        investigation_id=uuid4(),
        spec_hash="e" * 64,
        execution_revision=1,
    )
    service = InvestigationVerificationService()

    # 51 claims (> MAX_CLAIMS of 50)
    claims = tuple(
        InvestigationClaimV2(
            claim_id=uuid4(),
            statement=f"Claim {i}",
            kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        )
        for i in range(51)
    )

    outcome = await service.verify_claims(
        scope=scope,
        claims=claims,
        evidence_records=(),
    )

    with pytest.raises(ValueError, match="claims exceed 50 limit"):
        service.prepare_draft(
            scope=scope,
            outcome=outcome,
            claims=claims,
            evidence_refs=(),
        )

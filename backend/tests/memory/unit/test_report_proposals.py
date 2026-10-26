"""Only verified source observations can create bounded review candidates."""

from datetime import UTC, datetime
from uuid import UUID

from app.contracts.investigation_execution import (
    ClaimVerificationStatus,
    ClaimVerificationV1,
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationEvidenceV1,
    InvestigationReportV1,
    InvestigationStopReason,
)


def fixture(count=1, kind=InvestigationClaimKind.SOURCE_OBSERVATION):
    org, inv, snapshot, repo, actor = (UUID(int=n) for n in range(1, 6))
    records = tuple(
        InvestigationEvidenceV1(
            id=UUID(int=100 + n),
            org_id=org,
            investigation_id=inv,
            snapshot_id=snapshot,
            source_path="a.py",
            file_sha256="a" * 64,
            original_start_line=1,
            original_end_line=1,
            safe_start_line=1,
            safe_end_line=1,
            excerpt_sha256="b" * 64,
            observation_type="source",
        )
        for n in range(count)
    )
    claims = tuple(
        InvestigationClaimV2(
            claim_id=UUID(int=200 + n),
            statement="Observed source " + str(n),
            kind=kind,
            evidence_ids=(e.id,),
        )
        for n, e in enumerate(records)
    )
    checks = tuple(
        ClaimVerificationV1(
            claim_id=c.claim_id, status=ClaimVerificationStatus.SOURCE_CORRESPONDENCE_CONFIRMED
        )
        for c in claims
    )
    report = InvestigationReportV1(
        report_id=UUID(int=9),
        investigation_id=inv,
        spec_hash="c" * 64,
        runtime_revision=1,
        verdict="indeterminate",
        summary="Fixture",
        claims=claims,
        verifications=checks,
        evidence_refs=tuple(e.id for e in records),
        stop_reason=InvestigationStopReason.COMPLETED,
        report_digest="d" * 64,
        created_at=datetime.now(UTC),
    )
    return report, records, org, repo, snapshot, actor


def test_proposals_are_stable_capped_and_require_verified_observations():
    from app.services.report_memory import derive_report_memories

    args = fixture(12)
    proposals = derive_report_memories(*args)
    assert len(proposals) == 10
    assert proposals == derive_report_memories(*args)
    assert len({p.request_key for p in proposals}) == 10
    assert all(
        p.memory_kind == "investigation_episode" and len(p.evidence_pointers) == 1
        for p in proposals
    )
    assert derive_report_memories(*fixture(1, InvestigationClaimKind.SECURITY_HYPOTHESIS)) == ()
    assert derive_report_memories(*fixture(0)) == ()


def test_cross_snapshot_evidence_is_never_proposed():
    from app.services.report_memory import derive_report_memories

    report, records, org, repo, snapshot, actor = fixture()
    records = (records[0].model_copy(update={"snapshot_id": UUID(int=999)}),)
    assert derive_report_memories(report, records, org, repo, snapshot, actor) == ()

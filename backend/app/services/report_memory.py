"""Deterministic, source-bound proposals from retained investigation reports."""

from collections.abc import Sequence
from uuid import UUID

from app.contracts.investigation_execution import (
    ClaimVerificationStatus,
    InvestigationClaimKind,
    InvestigationEvidenceV1,
    InvestigationReportV1,
)
from app.contracts.memory import EvidencePointerV1, MemoryCreateRequestV1, MemoryKind
from app.domain.canonical_json import semantic_digest

DERIVATION_REVISION = 1


def derive_report_memories(
    report: InvestigationReportV1,
    evidence: Sequence[InvestigationEvidenceV1],
    org: UUID,
    repository: UUID,
    snapshot: UUID,
    actor: UUID,
) -> tuple[MemoryCreateRequestV1, ...]:
    """Propose at most ten mechanically supported observations, never accepted facts."""
    if not report.current_support_available:
        return ()
    confirmed = {
        v.claim_id
        for v in report.verifications
        if v.status == ClaimVerificationStatus.SOURCE_CORRESPONDENCE_CONFIRMED
    }
    allowed = {
        e.id: e
        for e in evidence
        if e.org_id == org
        and e.investigation_id == report.investigation_id
        and e.snapshot_id == snapshot
        and e.id in report.evidence_refs
    }
    proposals = []
    for claim in sorted(report.claims, key=lambda c: str(c.claim_id)):
        ids = tuple(sorted(set(claim.evidence_ids), key=str))
        if (
            claim.kind != InvestigationClaimKind.SOURCE_OBSERVATION
            or claim.claim_id not in confirmed
            or not 1 <= len(ids) <= 10
            or any(i not in allowed for i in ids)
        ):
            continue
        pointers = tuple(
            EvidencePointerV1(
                file_path=allowed[i].source_path,
                start_line=allowed[i].original_start_line,
                end_line=allowed[i].original_end_line,
                sha256=allowed[i].file_sha256,
            )
            for i in ids
        )
        summary = claim.statement
        if not summary.strip():
            continue
        key = semantic_digest(
            [
                str(report.report_id),
                DERIVATION_REVISION,
                str(claim.claim_id),
                summary,
                [p.model_dump(mode="json") for p in pointers],
            ]
        )
        proposals.append(
            MemoryCreateRequestV1(
                org_id=org,
                repository_id=repository,
                snapshot_id=snapshot,
                principal_id=actor,
                memory_kind=MemoryKind.INVESTIGATION_EPISODE,
                topic="Report source observation",
                summary=summary,
                request_key="report-memory:" + key,
                evidence_pointers=pointers,
            )
        )
        if len(proposals) == 10:
            break
    return tuple(proposals)

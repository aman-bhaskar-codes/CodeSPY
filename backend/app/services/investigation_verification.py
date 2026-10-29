"""Typed deterministic evidence verification and report preparation (Task H).

Validates model claims against pinned repository evidence, verifies source quotes,
enforces conservative verdict eligibility (unsupported security facts cannot be outvoted
by trivial quotes), rejects cross-snapshot citations and redacted secrets, and builds
immutable InvestigationReportDraftV1 drafts.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.contracts.investigation_execution import (
    ClaimVerificationStatus,
    ClaimVerificationV1,
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationEvidenceV1,
    InvestigationReportDraftV1,
    InvestigationScopeV1,
    InvestigationStopReason,
)
from app.domain.canonical_json import canonical_bytes

logger = logging.getLogger(__name__)

# Patterns indicating redacted secrets in quotes
_REDACTED_PATTERNS = [
    re.compile(r"\*{3,}REDACTED\*{3,}", re.IGNORECASE),
    re.compile(r"\[REDACTED\]", re.IGNORECASE),
    re.compile(r"\[SECRET\]", re.IGNORECASE),
    re.compile(r"ghp_[a-zA-Z0-9_\*]{10,}", re.IGNORECASE),
    re.compile(r"sk-[a-zA-Z0-9_\*]{10,}", re.IGNORECASE),
]


@dataclass(frozen=True)
class VerificationOutcomeV1:
    """Outcome of claim verification against repository evidence."""

    total_claims: int
    confirmed_count: int
    refuted_count: int
    unsupported_count: int
    grounding_ratio: float
    is_grounded: bool
    adjusted_verdict: str
    adjusted_confidence: float
    summary: str
    verifications: tuple[ClaimVerificationV1, ...]


class InvestigationVerificationService:
    """Deterministic server-side claim verification and report draft generator."""

    def __init__(self, minimum_grounding_ratio: float = 0.8) -> None:
        self.min_ratio = minimum_grounding_ratio

    async def verify_claims(
        self,
        scope: InvestigationScopeV1,
        claims: Sequence[InvestigationClaimV2],
        evidence_records: Sequence[InvestigationEvidenceV1] = (),
        file_resolver: Callable[[str], Awaitable[str | None]] | None = None,
        original_verdict: str | None = None,
        original_confidence: float | None = None,
        original_summary: str | None = None,
        coverage_notes: str = "",
        target_type: str = "finding",
        delivered_excerpts: Mapping[UUID, str] | None = None,
    ) -> VerificationOutcomeV1:
        # Rule 1: Zero claims -> grounding unavailable, conclusive verdict rejected
        if not claims:
            adjusted_verdict = original_verdict or "indeterminate"
            adjusted_confidence = min(original_confidence or 0.5, 0.4)
            if original_verdict in ("vulnerable", "true_positive", "benign", "false_positive"):
                adjusted_verdict = "indeterminate"

            return VerificationOutcomeV1(
                total_claims=0,
                confirmed_count=0,
                refuted_count=0,
                unsupported_count=0,
                grounding_ratio=0.0,
                is_grounded=False,
                adjusted_verdict=adjusted_verdict,
                adjusted_confidence=adjusted_confidence,
                summary=(
                    "No specific claims provided; grounding unavailable. "
                    "Conclusive verdict not permitted."
                ),
                verifications=(),
            )

        # Build evidence lookup by ID
        evidence_by_id = {e.id: e for e in evidence_records}

        verifications: list[ClaimVerificationV1] = []
        confirmed_cnt = 0
        refuted_cnt = 0
        unsupported_cnt = 0
        has_unsupported_security_hypothesis = False

        for claim in claims:
            v = await self._verify_single_claim(
                scope=scope,
                claim=claim,
                evidence_by_id=evidence_by_id,
                file_resolver=file_resolver,
                delivered_excerpts=delivered_excerpts,
            )
            verifications.append(v)

            if v.status == ClaimVerificationStatus.SOURCE_CORRESPONDENCE_CONFIRMED:
                confirmed_cnt += 1
            elif v.status == ClaimVerificationStatus.REFUTED:
                refuted_cnt += 1
                if claim.kind == InvestigationClaimKind.SECURITY_HYPOTHESIS:
                    has_unsupported_security_hypothesis = True
            else:
                unsupported_cnt += 1
                if claim.kind == InvestigationClaimKind.SECURITY_HYPOTHESIS:
                    has_unsupported_security_hypothesis = True

        total = len(claims)
        ratio = confirmed_cnt / total if total > 0 else 0.0

        # Rule 3: Conservative verdict eligibility
        # Even if grounding_ratio >= min_ratio, no security hypothesis may be unsupported/refuted
        is_grounded = (
            total > 0
            and refuted_cnt == 0
            and ratio >= self.min_ratio
            and not has_unsupported_security_hypothesis
        )

        adjusted_verdict = original_verdict or "indeterminate"
        adjusted_confidence = original_confidence if original_confidence is not None else 0.5

        if not is_grounded:
            if adjusted_verdict in ("vulnerable", "true_positive", "benign", "false_positive"):
                adjusted_verdict = "indeterminate"
                adjusted_confidence = min(adjusted_confidence, 0.4 if refuted_cnt == 0 else 0.3)

            if has_unsupported_security_hypothesis:
                summary_text = (
                    "Grounding failed: critical security hypotheses unsupported or refuted. "
                    f"Confirmed {confirmed_cnt}/{total} ({ratio:.0%}). "
                    "Verdict adjusted to indeterminate."
                )
            elif refuted_cnt > 0:
                summary_text = (
                    f"Grounding failed: {refuted_cnt} claims refuted against repository source. "
                    f"Confirmed {confirmed_cnt}/{total} ({ratio:.0%}). "
                    "Verdict adjusted to indeterminate."
                )
            else:
                summary_text = (
                    f"Grounding failed: {confirmed_cnt}/{total} claims confirmed ({ratio:.0%}) "
                    f"below minimum threshold {self.min_ratio:.0%}. "
                    "Verdict adjusted to indeterminate."
                )
        else:
            summary_text = (
                f"Grounding verified: {confirmed_cnt}/{total} claims confirmed against source "
                f"({ratio:.0%}) with zero refutations."
            )

        # Rule 4: benign/false_positive whole-repo limit
        if target_type == "repository" and adjusted_verdict in ("benign", "false_positive"):
            adjusted_verdict = "indeterminate"
            adjusted_confidence = min(adjusted_confidence, 0.4)
            summary_text += (
                " Partial or no-hit search cannot conclude whole-repository safety; "
                "verdict set to indeterminate."
            )

        return VerificationOutcomeV1(
            total_claims=total,
            confirmed_count=confirmed_cnt,
            refuted_count=refuted_cnt,
            unsupported_count=unsupported_cnt,
            grounding_ratio=ratio,
            is_grounded=is_grounded,
            adjusted_verdict=adjusted_verdict,
            adjusted_confidence=adjusted_confidence,
            summary=summary_text,
            verifications=tuple(verifications),
        )

    async def _verify_single_claim(
        self,
        scope: InvestigationScopeV1,
        claim: InvestigationClaimV2,
        evidence_by_id: dict[UUID, InvestigationEvidenceV1],
        file_resolver: Callable[[str], Awaitable[str | None]] | None,
        delivered_excerpts: Mapping[UUID, str] | None = None,
    ) -> ClaimVerificationV1:
        # Check cross-snapshot evidence references
        for eid in claim.evidence_ids:
            ev = evidence_by_id.get(eid)
            if ev is not None and ev.snapshot_id != scope.snapshot_id:
                return ClaimVerificationV1(
                    claim_id=claim.claim_id,
                    status=ClaimVerificationStatus.UNSUPPORTED,
                    notes=(
                        "Cross-snapshot citation rejected: evidence belongs to different snapshot."
                    ),
                )

        if not claim.suggested_file:
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.UNSUPPORTED,
                notes="Claim does not cite a specific repository file.",
            )

        file_path = claim.suggested_file.strip("/")
        if ".." in file_path.split("/"):
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.UNSUPPORTED,
                notes="Invalid file path: path traversal is not permitted.",
            )

        if delivered_excerpts is not None:
            for identity in claim.evidence_ids:
                evidence = evidence_by_id.get(identity)
                excerpt = delivered_excerpts.get(identity)
                if evidence is None or excerpt is None:
                    continue
                if (
                    evidence.org_id != scope.org_id
                    or evidence.investigation_id != scope.investigation_id
                    or evidence.snapshot_id != scope.snapshot_id
                    or evidence.source_path != file_path
                    or evidence.observation_type != "source_read"
                    or hashlib.sha256(excerpt.encode()).hexdigest() != evidence.excerpt_sha256
                ):
                    continue
                start, end = claim.suggested_line_start, claim.suggested_line_end
                quote = claim.suggested_quote
                if (
                    start is None
                    or end is None
                    or not quote
                    or end - start >= 200
                    or start < evidence.original_start_line
                    or end > evidence.original_end_line
                    or any(pattern.search(quote) for pattern in _REDACTED_PATTERNS)
                ):
                    continue
                numbered: dict[int, str] = {}
                for line in excerpt.splitlines():
                    prefix, separator, content = line.partition(": ")
                    if not separator or not prefix.isdigit() or int(prefix) in numbered:
                        numbered.clear()
                        break
                    numbered[int(prefix)] = content
                if any(line not in numbered for line in range(start, end + 1)):
                    continue
                source_slice = "\n".join(numbered[line] for line in range(start, end + 1))
                if quote in source_slice:
                    return ClaimVerificationV1(
                        claim_id=claim.claim_id,
                        status=ClaimVerificationStatus.SOURCE_CORRESPONDENCE_CONFIRMED,
                        resolved_file=file_path,
                        resolved_line_start=start,
                        resolved_line_end=end,
                        source_digest=hashlib.sha256(canonical_bytes(source_slice)).hexdigest(),
                        notes=(
                            "Exact quote matches delivered source; the conclusion remains a claim."
                        ),
                    )
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.UNSUPPORTED,
                notes="No delivered source receipt supports this quote and range.",
            )

        source_text: str | None = None
        if file_resolver is not None:
            source_text = await file_resolver(file_path)

        # If not resolved via file_resolver, check evidence records for matching source_path
        if source_text is None:
            matching_ev = [
                e
                for e in evidence_by_id.values()
                if e.source_path == file_path and e.snapshot_id == scope.snapshot_id
            ]
            if not matching_ev:
                return ClaimVerificationV1(
                    claim_id=claim.claim_id,
                    status=ClaimVerificationStatus.UNSUPPORTED,
                    resolved_file=file_path,
                    notes=f"File '{file_path}' was not found in the repository snapshot.",
                )

        if source_text is None:
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.UNSUPPORTED,
                resolved_file=file_path,
                notes=f"Source content for '{file_path}' was unavailable for verification.",
            )

        lines = source_text.splitlines()
        total_lines = len(lines)

        # Line bounds check
        start_line = claim.suggested_line_start or 1
        default_end = start_line if claim.suggested_line_start else total_lines
        end_line = claim.suggested_line_end or default_end

        if (
            start_line < 1
            or start_line > max(1, total_lines)
            or end_line < start_line
            or end_line > max(1, total_lines)
        ):
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.UNSUPPORTED,
                resolved_file=file_path,
                resolved_line_start=start_line,
                resolved_line_end=end_line,
                notes=(
                    f"Line range {start_line}-{end_line} invalid for file with {total_lines} lines."
                ),
            )

        # Quoted text check
        if not claim.suggested_quote:
            # Rule 2: Quote-free file existence proves location validity, not claim truth.
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.UNSUPPORTED,
                resolved_file=file_path,
                resolved_line_start=start_line,
                resolved_line_end=end_line,
                notes=(
                    f"File/location exists at {file_path}:{start_line}, "
                    "but claim lacks supporting quote or structured fact."
                ),
            )

        # Redacted secret check
        for pattern in _REDACTED_PATTERNS:
            if pattern.search(claim.suggested_quote):
                return ClaimVerificationV1(
                    claim_id=claim.claim_id,
                    status=ClaimVerificationStatus.UNSUPPORTED,
                    resolved_file=file_path,
                    resolved_line_start=start_line,
                    resolved_line_end=end_line,
                    notes=(
                        "Cannot ground claim against redacted secret content or placeholder tokens."
                    ),
                )

        target_slice = "\n".join(lines[start_line - 1 : end_line])
        norm_quote = " ".join(claim.suggested_quote.strip().split())
        norm_slice = " ".join(target_slice.strip().split())

        if norm_quote in norm_slice:
            source_digest = hashlib.sha256(canonical_bytes(target_slice)).hexdigest()
            notes = (
                "Source correspondence confirmed; security hypothesis retains inferred status."
                if claim.kind == InvestigationClaimKind.SECURITY_HYPOTHESIS
                else f"Quoted code confirmed at {file_path}:{start_line}."
            )
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.SOURCE_CORRESPONDENCE_CONFIRMED,
                resolved_file=file_path,
                resolved_line_start=start_line,
                resolved_line_end=end_line,
                source_digest=source_digest,
                notes=notes,
            )

        # Check if quote exists elsewhere in file
        norm_file = " ".join(source_text.strip().split())
        if norm_quote in norm_file:
            return ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.LOCATION_VALID_QUOTE_MISSING,
                resolved_file=file_path,
                resolved_line_start=start_line,
                resolved_line_end=end_line,
                notes=(
                    f"Quoted text exists elsewhere in '{file_path}', "
                    f"not in cited line range {start_line}-{end_line}."
                ),
            )

        return ClaimVerificationV1(
            claim_id=claim.claim_id,
            status=ClaimVerificationStatus.REFUTED,
            resolved_file=file_path,
            resolved_line_start=start_line,
            resolved_line_end=end_line,
            notes=f"Quoted text not found in '{file_path}'.",
        )

    def prepare_draft(
        self,
        scope: InvestigationScopeV1,
        outcome: VerificationOutcomeV1,
        claims: Sequence[InvestigationClaimV2],
        evidence_refs: Sequence[UUID] = (),
        coverage_notes: str = "",
        unknowns: str = "",
        limitations: str = "",
        settled_usage_micros: int = 0,
        held_usage_micros: int = 0,
        stop_reason: InvestigationStopReason = InvestigationStopReason.COMPLETED,
        current_support_available: bool = True,
    ) -> InvestigationReportDraftV1:
        """Constructs and returns an InvestigationReportDraftV1 with calculated digest."""
        draft_dict: dict[str, Any] = {
            "investigation_id": scope.investigation_id,
            "spec_hash": scope.spec_hash,
            "verdict": outcome.adjusted_verdict,
            "verdict_confidence": outcome.adjusted_confidence,
            "summary": outcome.summary[:8192],
            "claims": tuple(claims),
            "verifications": outcome.verifications,
            "evidence_refs": tuple(evidence_refs),
            "coverage_notes": coverage_notes[:4096],
            "unknowns": unknowns[:4096],
            "limitations": limitations[:4096],
            "settled_usage_micros": max(0, settled_usage_micros),
            "held_usage_micros": max(0, held_usage_micros),
            "stop_reason": stop_reason,
            "current_support_available": current_support_available,
        }

        draft_dict["draft_digest"] = "0" * 64
        draft = InvestigationReportDraftV1.model_validate(draft_dict)
        digest = draft.compute_draft_digest()
        return draft.model_copy(update={"draft_digest": digest})

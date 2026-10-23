"""Deterministic claim verification engine: validates LLM claims against ground truth."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.contracts.investigation import (
    InvestigationClaimV1,
    InvestigationVerdict,
)
from app.services.investigator_tools import ToolExecutionContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimVerificationReportV1:
    total_claims: int
    verified_count: int
    refuted_count: int
    unsupported_count: int
    grounding_ratio: float
    is_grounded: bool
    verified_claims: tuple[InvestigationClaimV1, ...]
    adjusted_verdict: str | None
    adjusted_confidence: float | None
    verification_summary: str


class ClaimVerificationService:
    """Verifies investigator claims against actual repository contents to prevent hallucinations."""

    def __init__(self, minimum_grounding_ratio: float = 0.8) -> None:
        self.min_ratio = minimum_grounding_ratio

    async def verify_claims(
        self,
        claims: Sequence[InvestigationClaimV1],
        context: ToolExecutionContext,
        original_verdict: str | None = None,
        original_confidence: float | None = None,
    ) -> ClaimVerificationReportV1:
        if not claims:
            # If no claims were extracted, cannot verify evidence grounding
            return ClaimVerificationReportV1(
                total_claims=0,
                verified_count=0,
                refuted_count=0,
                unsupported_count=0,
                grounding_ratio=1.0,
                is_grounded=True,
                verified_claims=(),
                adjusted_verdict=original_verdict,
                adjusted_confidence=original_confidence,
                verification_summary="No specific claims to verify; grounding unconstrained.",
            )

        updated_claims: list[InvestigationClaimV1] = []
        verified_cnt = 0
        refuted_cnt = 0
        unsupported_cnt = 0

        for claim in claims:
            status, notes = await self._verify_single_claim(claim, context)
            if status == "verified":
                verified_cnt += 1
            elif status == "refuted":
                refuted_cnt += 1
            else:
                unsupported_cnt += 1

            updated_claims.append(
                claim.model_copy(update={"status": status, "verification_notes": notes})
            )

        total = len(claims)
        ratio = verified_cnt / total if total > 0 else 0.0
        # A finding is grounded if ratio >= min_ratio AND no claim is outright refuted
        is_grounded = (ratio >= self.min_ratio) and (refuted_cnt == 0)

        adjusted_verdict = original_verdict
        adjusted_confidence = original_confidence

        if not is_grounded:
            logger.warning(
                "Investigation claims failed grounding check: ratio=%.2f (min=%.2f), refuted=%d",
                ratio,
                self.min_ratio,
                refuted_cnt,
            )
            adjusted_verdict = InvestigationVerdict.INDETERMINATE.value
            adjusted_confidence = min(original_confidence or 0.5, 0.4)
            summary = (
                f"Grounding verification failed: only {verified_cnt}/{total} claims verified "
                f"({ratio:.0%}). Refuted: {refuted_cnt}, Unsupported: {unsupported_cnt}. "
                "Verdict downgraded to indeterminate."
            )
        else:
            summary = (
                f"Grounding verified: {verified_cnt}/{total} claims confirmed against source "
                f"({ratio:.0%}) with zero refutations."
            )

        return ClaimVerificationReportV1(
            total_claims=total,
            verified_count=verified_cnt,
            refuted_count=refuted_cnt,
            unsupported_count=unsupported_cnt,
            grounding_ratio=ratio,
            is_grounded=is_grounded,
            verified_claims=tuple(updated_claims),
            adjusted_verdict=adjusted_verdict,
            adjusted_confidence=adjusted_confidence,
            verification_summary=summary,
        )

    async def _verify_single_claim(
        self, claim: InvestigationClaimV1, context: ToolExecutionContext
    ) -> tuple[str, str]:
        # Rule 1: Claim must specify file_path
        if not claim.file_path:
            return "unsupported", "Claim does not cite a specific repository file."

        file_path = claim.file_path.strip("/")

        # Rule 2: Fetch source content
        source_text: str | None = None
        if context.mock_files and file_path in context.mock_files:
            source_text = context.mock_files[file_path]

        if source_text is None:
            return "unsupported", f"File '{file_path}' was not found in the repository."

        lines = source_text.splitlines()
        total_lines = len(lines)

        # Rule 3: Check line numbers if specified
        if claim.line_start is not None:
            if claim.line_start < 1 or claim.line_start > total_lines:
                return (
                    "unsupported",
                    f"Cited line {claim.line_start} is out of bounds ({total_lines} lines).",
                )

            end_line = claim.line_end if claim.line_end is not None else claim.line_start
            if end_line < claim.line_start or end_line > total_lines:
                return (
                    "unsupported",
                    f"Line range {claim.line_start}-{end_line} invalid for {total_lines} lines.",
                )

            target_slice = "\n".join(lines[claim.line_start - 1 : end_line])
        else:
            target_slice = source_text

        # Rule 4: If quoted_source is given, verify it matches
        if claim.quoted_source:
            norm_quote = " ".join(claim.quoted_source.strip().split())
            norm_target = " ".join(target_slice.strip().split())

            if norm_quote in norm_target:
                return "verified", f"Quoted code confirmed at {file_path}:{claim.line_start or 1}."

            # Check if quote exists anywhere else in file (possible line drift)
            norm_file = " ".join(source_text.strip().split())
            if norm_quote in norm_file:
                return (
                    "unsupported",
                    f"Code in '{file_path}' does not match cited lines "
                    f"{claim.line_start}-{claim.line_end}.",
                )

            return (
                "refuted",
                f"Quoted code '{claim.quoted_source[:40]}...' does not exist in '{file_path}'.",
            )

        # No quoted source, but file and line range exist and are valid
        return "verified", f"File and lines verified at {file_path}:{claim.line_start or 1}."

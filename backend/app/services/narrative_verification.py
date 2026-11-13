"""Deterministic verification of a report narrative against the statements it was shown.

A sentence is confirmed only if every statement it cites was delivered to this investigation as a
``report_fact`` receipt of the pinned report, and the numbers and backticked names in the sentence
appear in what those statements say. Statements of kind ``unknown`` can only support a sentence
that says something is unknown. There is no model in this check and no source evidence is accepted
here: ``report_fact`` receipts are the only support a narrative sentence can have.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from app.contracts.investigation_execution import InvestigationEvidenceV1
from app.contracts.report_narrative import (
    NarrativeBasis,
    NarrativeConclusionV1,
    NarrativeOutcomeV1,
    NarrativeSentenceV1,
    NarrativeVerificationV1,
)

GROUNDED_RATIO = 0.8
_NAME = re.compile(r"`([^`\n]{1,200})`")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")
_UNKNOWN_WORDS = (
    "unknown",
    "could not",
    "cannot",
    "can't",
    "not known",
    "unresolved",
    "unlocated",
    "not analyzed",
    "not analysed",
    "no evidence",
    "not determined",
    "not observed",
    "uncertain",
)
_STRENGTH: dict[str, NarrativeBasis] = {
    "observation": "observed",
    "derivation": "derived",
    "inference": "inferred",
    "unknown": "unknown",
}
_ORDER = ("observed", "derived", "inferred", "unknown")


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUMBER.findall(text)}


def _statement_text(entry: Mapping[str, Any]) -> str:
    params = entry.get("params") or {}
    values = [str(v) for v in params.values()]
    return "\n".join([str(entry.get("text", "")), *values])


def delivered_statements(
    *,
    evidence: Sequence[InvestigationEvidenceV1],
    delivered: Mapping[UUID, str],
    org_id: UUID,
    investigation_id: UUID,
    snapshot_id: UUID,
    report_digest: str,
) -> dict[str, dict[str, Any]]:
    """The statements this investigation was actually shown, by sid, from its own receipts."""
    shown: dict[str, dict[str, Any]] = {}
    prefix = f"{report_digest}:"
    for row in evidence:
        if (
            row.observation_type != "report_fact"
            or row.org_id != org_id
            or row.investigation_id != investigation_id
            or row.snapshot_id != snapshot_id
            or row.fact_ref is None
            or not row.fact_ref.startswith(prefix)
        ):
            continue
        page = delivered.get(row.id)
        if page is None or hashlib.sha256(page.encode("utf-8")).hexdigest() != row.excerpt_sha256:
            continue
        sid = row.fact_ref[len(prefix) :]
        try:
            statements = json.loads(page)["statements"]
        except (ValueError, KeyError, TypeError):
            continue
        for entry in statements:
            if isinstance(entry, dict) and entry.get("sid") == sid:
                shown[sid] = entry
    return shown


def _verify_sentence(
    sentence: NarrativeSentenceV1, shown: Mapping[str, Mapping[str, Any]]
) -> NarrativeVerificationV1:
    def no(reason: str) -> NarrativeVerificationV1:
        return NarrativeVerificationV1(
            claim_id=sentence.claim_id, status="unsupported", reason=reason
        )

    if not sentence.sids:
        return no("the sentence cites no statement")
    cited = [shown.get(sid) for sid in sentence.sids]
    if any(entry is None for entry in cited):
        return no("a cited statement was not shown to this investigation")
    entries = [entry for entry in cited if entry is not None]
    haystack = "\n".join(_statement_text(entry) for entry in entries)
    allowed_numbers = _numbers(haystack)
    names = _NAME.findall(sentence.text)
    for name in names:
        if name not in haystack:
            return no(f"`{name}` does not appear in the cited statements")
    prose = _NAME.sub(" ", sentence.text)
    for number in sorted(_numbers(prose)):
        if number not in allowed_numbers:
            return no(f"the number {number} does not appear in the cited statements")
    kinds = {str(entry.get("kind")) for entry in entries}
    lowered = sentence.text.lower()
    if "unknown" in kinds and not any(word in lowered for word in _UNKNOWN_WORDS):
        return no("an unknown statement cannot support a definite claim")
    weakest = max((_STRENGTH.get(k, "inferred") for k in kinds), key=_ORDER.index)
    return NarrativeVerificationV1(claim_id=sentence.claim_id, status="confirmed", basis=weakest)


def verify_narrative(
    conclusion: NarrativeConclusionV1, shown: Mapping[str, Mapping[str, Any]]
) -> NarrativeOutcomeV1:
    results = tuple(_verify_sentence(s, shown) for s in conclusion.sentences)
    confirmed = sum(1 for r in results if r.status == "confirmed")
    total = len(results)
    return NarrativeOutcomeV1(
        verifications=results,
        confirmed=confirmed,
        total=total,
        grounded=total > 0 and confirmed / total >= GROUNDED_RATIO,
    )


def render_narrative(conclusion: NarrativeConclusionV1, outcome: NarrativeOutcomeV1) -> str:
    """Plain text of the narrative; a sentence that was not confirmed is marked, never hidden."""
    status = {v.claim_id: v for v in outcome.verifications}
    lines = [conclusion.overview] if conclusion.overview else []
    for sentence in conclusion.sentences:
        verdict = status[sentence.claim_id]
        lines.append(
            sentence.text if verdict.status == "confirmed" else f"[unverified] {sentence.text}"
        )
    return "\n".join(lines)[:8192]

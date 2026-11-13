"""Contracts of a report narrative: what the model writes and what code concludes about it.

The model writes sentences that each cite statements of one pinned architecture report by id.
It never decides whether a sentence is supported; ``services/narrative_verification`` does,
from the statements the investigation was actually shown.
"""

from typing import Literal

from pydantic import Field, model_validator

from app.domain.scope import Contract

MAX_SENTENCES = 50
MAX_SIDS_PER_SENTENCE = 8
SID_PATTERN = r"^[0-9a-f]{16}$"

NarrativeBasis = Literal["observed", "derived", "inferred", "unknown"]
NarrativeStatus = Literal["confirmed", "unsupported"]


class NarrativeSentenceV1(Contract):
    """One sentence of the narrative and the report statements it cites."""

    claim_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    text: str = Field(min_length=1, max_length=600)
    sids: tuple[str, ...] = Field(default=(), max_length=MAX_SIDS_PER_SENTENCE)

    @model_validator(mode="after")
    def _sids_are_ids(self) -> "NarrativeSentenceV1":
        import re

        if any(not re.fullmatch(SID_PATTERN, sid) for sid in self.sids):
            raise ValueError("a cited statement id is 16 lowercase hex characters")
        if len(set(self.sids)) != len(self.sids):
            raise ValueError("a statement is cited once per sentence")
        return self


class NarrativeConclusionV1(Contract):
    """The schema of the model's reply: an overview and cited sentences."""

    schema_version: Literal[1] = 1
    overview: str = Field(default="", max_length=1200)
    sentences: tuple[NarrativeSentenceV1, ...] = Field(default=(), max_length=MAX_SENTENCES)

    @model_validator(mode="after")
    def _unique_claims(self) -> "NarrativeConclusionV1":
        ids = [s.claim_id for s in self.sentences]
        if len(set(ids)) != len(ids):
            raise ValueError("claim ids are unique")
        return self


class NarrativeVerificationV1(Contract):
    claim_id: str
    status: NarrativeStatus
    basis: NarrativeBasis | None = (
        None  # the weakest kind among the cited statements, when confirmed
    )
    reason: str = Field(default="", max_length=300)


class NarrativeOutcomeV1(Contract):
    """What verification concluded; whatever is not confirmed is shown as unverified."""

    verifications: tuple[NarrativeVerificationV1, ...]
    confirmed: int = Field(ge=0)
    total: int = Field(ge=0)
    grounded: bool

    @property
    def ratio(self) -> float:
        return self.confirmed / self.total if self.total else 0.0

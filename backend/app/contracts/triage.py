"""Triage contracts for human review and state transition validation."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

TriageState = Literal[
    "unreviewed",
    "confirmed",
    "false_positive",
    "mitigated",
    "accepted_risk",
]

TriageReason = Literal[
    "none",
    "code_refactor",
    "compensating_control",
    "business_context",
    "test_code",
    "vendor_dependency",
]


class TriageTransitionV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: TriageState
    reason: TriageReason = "none"
    expected_version: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_reason_requirement(self) -> "TriageTransitionV1":
        if self.state in {"false_positive", "mitigated", "accepted_risk"} and self.reason == "none":
            raise ValueError(f"reason must be provided when transitioning to {self.state}")
        return self


class TriageRecordV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: UUID
    current_state: TriageState
    current_reason: TriageReason
    last_event_id: UUID | None = None
    updated_by: UUID | None = None
    updated_at: datetime | None = None
    version: int = Field(ge=0)


class TriageHistoryItemV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    finding_id: UUID
    principal_id: UUID
    state: TriageState
    reason: TriageReason
    created_at: datetime


class TriageHistoryV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: UUID
    events: list[TriageHistoryItemV1] = Field(default_factory=list)
    next_cursor: str | None = None

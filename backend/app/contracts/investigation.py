"""Versioned contracts for investigation domain, state snapshots, claims, and event streams."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.model import ChatMessageV1


class InvestigationTargetType(StrEnum):
    FINDING = "finding"
    REPOSITORY = "repository"
    COMMIT = "commit"
    DIFF = "diff"


class InvestigationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    CHECKPOINTED = "checkpointed"
    AWAITING_HUMAN_DECISION = "awaiting_human_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvestigationMode(StrEnum):
    SHORT_PATH = "short_path"
    TWO_SPECIALIST = "two_specialist"


class InvestigationDepth(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class InvestigationVerdict(StrEnum):
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    INDETERMINATE = "indeterminate"
    BENIGN = "benign"


class InvestigationEventType(StrEnum):
    STEP_STARTED = "step_started"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    CHECKPOINT_SAVED = "checkpoint_saved"
    CLAIM_EXTRACTED = "claim_extracted"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_RESULT = "verification_result"
    VERDICT_REACHED = "verdict_reached"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvestigationClaimV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    statement: str
    quoted_source: str | None = None
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    status: str = "pending"  # "pending", "verified", "refuted", "unsupported"
    verification_notes: str | None = None


class InvestigationStateV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    investigation_id: UUID
    org_id: UUID
    repository_id: UUID
    finding_id: UUID | None = None
    target_type: str
    query: str
    mode: str = "short_path"
    depth: str = "standard"
    current_step: int = 0
    max_steps: int = 10
    history: tuple[ChatMessageV1, ...] = ()
    tool_history: tuple[dict[str, Any], ...] = ()
    collected_evidence: tuple[dict[str, Any], ...] = ()
    claims: tuple[InvestigationClaimV1, ...] = ()
    verdict: str | None = None
    verdict_confidence: float | None = None
    summary: str | None = None
    total_spent_micros: int = 0
    human_guidance: str | None = None


class InvestigationCreateRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    target_type: InvestigationTargetType = InvestigationTargetType.FINDING
    finding_id: UUID | None = None
    mode: InvestigationMode = InvestigationMode.SHORT_PATH
    depth: InvestigationDepth = InvestigationDepth.STANDARD
    max_steps: int = Field(default=10, ge=1, le=25)
    budget_limit_micros: int = Field(default=5_000_000, gt=0)  # $5.00 limit


class InvestigationCheckpointV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: UUID
    investigation_id: UUID
    step_index: int
    fencing_token: int
    worker_id: str
    graph_node: str
    state: InvestigationStateV1
    created_at: datetime


class InvestigationEventV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    org_id: UUID
    investigation_id: UUID
    step_index: int
    event_type: str
    node_name: str
    payload: dict[str, Any]
    created_at: datetime


class InvestigationSummaryV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    org_id: UUID
    repository_id: UUID
    finding_id: UUID | None = None
    target_type: str
    query: str
    status: str
    mode: str
    depth: str
    current_step: int
    max_steps: int
    fencing_token: int
    worker_id: str | None = None
    budget_limit_micros: int
    spent_micros: int
    verdict: str | None = None
    verdict_confidence: float | None = None
    summary: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class InvestigationDetailResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: InvestigationSummaryV1
    latest_state: InvestigationStateV1 | None = None
    claims: tuple[InvestigationClaimV1, ...] = ()
    events_count: int = 0


class InvestigationFeedbackRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    feedback_type: str = Field(description="'agree', 'disagree', 'needs_review', or 'comment'")
    notes: str = Field(min_length=1, max_length=4000)
    save_to_memory: bool = False
    memory_topic: str | None = None


class InvestigationListResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[InvestigationSummaryV1, ...]
    total_count: int

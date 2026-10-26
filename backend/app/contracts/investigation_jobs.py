"""Versioned contracts for investigation job lifecycle.

Covers enqueue, envelope, start/cancel/resume/retry commands,
query responses and job projections. Extends the existing job
system with investigate_snapshot operation support.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.contracts.investigation import (
    InvestigationDepth,
    InvestigationMode,
    InvestigationStatus,
    InvestigationSummaryV1,
)
from app.domain.canonical_json import canonical_bytes
from app.domain.scope import Contract

# ---------------------------------------------------------------------------
# Envelope (queue transport — no source/model content)
# ---------------------------------------------------------------------------


class JobEnvelopeV6(Contract):
    """Queue envelope for investigate_snapshot jobs.

    Carries identity/config/deadline only; no source or model content.
    Schema version 6 to distinguish from existing V2/V3 envelopes.
    """

    schema_version: Literal[6] = 6
    org_id: UUID
    job_id: UUID
    operation: Literal[
        "admit_source",
        "inventory_snapshot",
        "analyze_structure",
        "investigate_snapshot",
    ]
    dispatch_generation: int = Field(gt=0)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deadline: AwareDatetime
    investigation_id: UUID | None = None
    spec_hash: str | None = Field(default=None, pattern=r"^([0-9a-f]{64})?$")

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > 4096:
            raise ValueError("queue envelope exceeds 4096 byte bound")
        return self

    @model_validator(mode="after")
    def _operation_fields(self) -> Self:
        if self.operation == "investigate_snapshot":
            if self.investigation_id is None:
                raise ValueError("investigate_snapshot requires investigation_id")
            if self.spec_hash is None:
                raise ValueError("investigate_snapshot requires spec_hash")
        return self


# ---------------------------------------------------------------------------
# Enqueue command
# ---------------------------------------------------------------------------


class EnqueueInvestigationJobV1(Contract):
    """Request to create a durable investigation job."""

    repository_id: UUID
    snapshot_id: UUID
    investigation_id: UUID
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["investigate_snapshot"] = "investigate_snapshot"
    request_key: str = Field(min_length=1, max_length=128)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_of_job_id: UUID | None = None

    @model_validator(mode="after")
    def _key_bytes(self) -> Self:
        if len(self.request_key.encode("utf-8")) > 128:
            raise ValueError("request key exceeds 128 byte bound")
        return self


# ---------------------------------------------------------------------------
# Job projection
# ---------------------------------------------------------------------------


class InvestigationJobRecordV1(Contract):
    """Projection of actual jobs columns for investigation operations."""

    org_id: UUID
    id: UUID
    repository_id: UUID
    investigation_id: UUID
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["investigate_snapshot"]
    state: Literal[
        "queued",
        "running",
        "retry_wait",
        "cancelling",
        "succeeded",
        "partial",
        "failed",
        "cancelled",
    ]
    attempt_count: int
    max_attempts: int
    deadline: AwareDatetime
    failure_code: str | None = None
    report_id: UUID | None = None
    created_at: AwareDatetime
    finished_at: AwareDatetime | None = None


# ---------------------------------------------------------------------------
# Create command — V2 with required snapshot
# ---------------------------------------------------------------------------


class InvestigationCreateRequestV2(Contract):
    """Create request requiring an immutable snapshot binding."""

    repository_id: UUID
    snapshot_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    target_type: Literal["repository", "finding"]
    finding_id: UUID | None = None
    finding_run_id: UUID | None = None
    chunk_set_id: UUID | None = None
    mode: InvestigationMode = InvestigationMode.SHORT_PATH
    depth: InvestigationDepth = InvestigationDepth.STANDARD
    max_steps: int = Field(default=10, ge=1, le=25)
    budget_limit_micros: int = Field(default=5_000_000, gt=0)

    @field_validator("query")
    @classmethod
    def _query_bytes(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 16_384:
            raise ValueError("query exceeds 16384 byte limit")
        return v

    @model_validator(mode="after")
    def _target_validation(self) -> Self:
        if self.target_type == "finding":
            if self.finding_id is None:
                raise ValueError("finding target requires finding_id")
            if self.finding_run_id is None:
                raise ValueError("finding target requires finding_run_id")
        if self.target_type == "repository" and self.finding_id is not None:
            raise ValueError("repository target cannot have finding_id")
        # Reject commit/diff until they resolve to admitted snapshots
        return self


# ---------------------------------------------------------------------------
# Start response
# ---------------------------------------------------------------------------


class InvestigationStartResponseV1(Contract):
    """202 Accepted response after starting an investigation job."""

    investigation_id: UUID
    job_id: UUID
    status: InvestigationStatus
    status_url: str = Field(min_length=1, max_length=2048)
    events_url: str = Field(min_length=1, max_length=2048)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class InvestigationCancelResponseV1(Contract):
    """Response to a cancel command."""

    investigation_id: UUID
    job_id: UUID | None = None
    status: InvestigationStatus
    already_terminal: bool = False


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class InvestigationResumeRequestV1(Contract):
    """Resume request — only after awaiting_human_decision with resolution."""

    expected_failed_job_id: UUID
    expected_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class InvestigationRetryRequestV1(Contract):
    """Retry request — creates a new investigation with retry_of lineage."""

    max_steps: int | None = Field(default=None, ge=1, le=25)
    budget_limit_micros: int | None = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# Query responses — V2 with job/report support
# ---------------------------------------------------------------------------


class InvestigationSummaryV2(Contract):
    """Extended summary with job/spec/report references."""

    id: UUID
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID | None = None
    finding_id: UUID | None = None
    target_type: str
    query: str
    status: str
    mode: str
    depth: str
    current_step: int
    max_steps: int
    budget_limit_micros: int
    spent_micros: int
    held_micros: int = 0
    verdict: str | None = None
    verdict_confidence: float | None = None
    summary: str | None = None
    failure_reason: str | None = None
    spec_hash: str | None = None
    current_job_id: UUID | None = None
    report_id: UUID | None = None
    execution_revision: int | None = None
    retry_of_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    def to_v1(self) -> InvestigationSummaryV1:
        """Backward-compatible projection for legacy clients."""
        return InvestigationSummaryV1(
            id=self.id,
            org_id=self.org_id,
            repository_id=self.repository_id,
            finding_id=self.finding_id,
            target_type=self.target_type,
            query=self.query,
            status=self.status,
            mode=self.mode,
            depth=self.depth,
            current_step=self.current_step,
            max_steps=self.max_steps,
            fencing_token=0,  # Legacy field
            worker_id=None,
            budget_limit_micros=self.budget_limit_micros,
            spent_micros=self.spent_micros,
            verdict=self.verdict,
            verdict_confidence=self.verdict_confidence,
            summary=self.summary,
            failure_reason=self.failure_reason,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class InvestigationDetailResponseV2(Contract):
    """Extended detail response with job, report and usage information."""

    summary: InvestigationSummaryV2
    job: InvestigationJobRecordV1 | None = None
    report: dict[str, Any] | None = None  # Serialized InvestigationReportV1 when available
    claims_count: int = 0
    evidence_count: int = 0
    events_count: int = 0
    available_actions: tuple[str, ...] = ()
    current_support_available: bool = True


class InvestigationListResponseV2(Contract):
    """Paginated investigation list."""

    items: tuple[InvestigationSummaryV2, ...]
    total_count: int
    next_cursor: str | None = None


class InvestigationEventV2(Contract):
    """Monotonically ordered event with sequence identity."""

    id: UUID
    org_id: UUID
    investigation_id: UUID
    sequence: int = Field(ge=0)
    step_index: int
    event_type: str
    node_name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    logical_event_key: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _bounded_payload(self) -> Self:
        if len(canonical_bytes(self.payload)) > 4096:
            raise ValueError("event payload exceeds 4096 byte bound")
        return self


class InvestigationEventPageV1(Contract):
    """Paginated event response with cursor support."""

    items: tuple[InvestigationEventV2, ...]
    cursor_floor: int = 0
    next_cursor: int | None = None
    has_gap: bool = False

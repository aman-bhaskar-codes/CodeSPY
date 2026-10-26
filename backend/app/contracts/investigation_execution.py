"""Versioned contracts for durable investigation execution.

Covers investigation specification, scopes, limits, node identities,
tool request/result, report/publication and execution state.
All contracts are frozen, strict and extra-forbid.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.domain.canonical_json import canonical_bytes
from app.domain.scope import Contract

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INVESTIGATION_RUNTIME_REVISION: Literal[1] = 1
"""Bumped when node semantics, tool contracts or report format change."""

MAX_SPEC_BYTES: int = 32_768
MAX_QUERY_CHARS: int = 4000
MAX_QUERY_BYTES: int = 16_384
MAX_STEPS: int = 25
DEFAULT_STEPS: int = 10
MAX_TOOL_CALLS_PER_RESPONSE: int = 16
MAX_TOOL_CALLS_PER_INVESTIGATION: int = 40
MAX_SEARCH_HITS: int = 20
DEFAULT_SEARCH_HITS: int = 5
MAX_MEMORY_HITS: int = 10
DEFAULT_MEMORY_HITS: int = 5
MAX_SOURCE_READ_LINES: int = 200
MAX_SAFE_RETURNED_BYTES: int = 32_768
MAX_TOOL_ARG_BYTES: int = 16_384
MAX_TOOL_QUERY_BYTES: int = 1024
MAX_TOOL_PAYLOAD_BYTES: int = 32_768
MAX_RETAINED_TOOL_BYTES: int = 262_144  # 256 KiB total per investigation
MAX_CONTEXT_TOKENS: int = 8192
DEFAULT_CONTEXT_TOKENS: int = 4096
MAX_CONTEXT_BYTES: int = 65_536
DEFAULT_CONTEXT_BYTES: int = 32_768
MAX_MODEL_REQUEST_BYTES: int = 1_048_576  # 1 MiB
MAX_MODEL_RESPONSE_BYTES: int = 2_097_152  # 2 MiB
MAX_RETAINED_RESULT_BYTES: int = 524_288  # 512 KiB
MAX_CHECKPOINT_BYTES: int = 524_288  # 512 KiB
MAX_REPORT_BYTES: int = 262_144  # 256 KiB
MAX_CLAIMS: int = 50
MAX_EVIDENCE_REFS: int = 200
MAX_CLAIM_STATEMENT_CHARS: int = 2000
MAX_MODEL_CALL_DURATION_SECONDS: int = 120
MAX_TOOL_DURATION_SECONDS: int = 10
INVESTIGATION_DEADLINE_SECONDS: int = 900  # 15 minutes
MAX_AUTOMATIC_ATTEMPTS: int = 3
FINALIZATION_TOTAL_SECONDS: int = 10
FINALIZATION_LOCK_TIMEOUT_SECONDS: int = 2
FINALIZATION_STATEMENT_TIMEOUT_SECONDS: int = 5
MAX_EVENTS_PER_PAGE: int = 100
MAX_EVENT_PAYLOAD_BYTES: int = 4096
RESULT_RETENTION_DAYS: int = 7
RESULT_CLEANUP_BATCH_SIZE: int = 100
MAX_RECOVERY_CANDIDATES: int = 100

# ---------------------------------------------------------------------------
# Namespace for deterministic UUID5 generation
# ---------------------------------------------------------------------------

_INVESTIGATION_NS = UUID("b1c2d3e4-f5a6-7890-abcd-ef1234567890")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InvestigationNodeKind(StrEnum):
    """Logical node types within an investigation execution."""

    GATHER = "gather"
    INVESTIGATE = "investigate"
    PLAN = "plan"
    EVIDENCE = "evidence"
    AUDIT = "audit"
    SYNTHESIZE = "synthesize"
    VERIFY = "verify"
    FINALIZE = "finalize"


class InvestigationToolName(StrEnum):
    """Allowed investigation tool identifiers."""

    SEARCH_CODE = "search_code"
    READ_FILE_RANGE = "read_file_range"
    CHECK_MEMORY = "check_memory"
    INSPECT_FINDING = "inspect_finding"
    TRACE_CONTEXT = "trace_context"


class InvestigationToolStatus(StrEnum):
    """Outcome status for a tool invocation."""

    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    INVALID = "invalid"
    TIMED_OUT = "timed_out"


class InvestigationAdvanceStatus(StrEnum):
    """Executor advance outcome."""

    PROGRESS = "progress"
    READY_TO_PUBLISH = "ready_to_publish"
    NEEDS_REVIEW = "needs_review"
    TERMINAL_FAILURE = "terminal_failure"


class InvestigationClaimKind(StrEnum):
    """Typed claim categories for evidence grounding."""

    SOURCE_OBSERVATION = "source_observation"
    SECURITY_HYPOTHESIS = "security_hypothesis"
    COVERAGE_STATEMENT = "coverage_statement"


class ModelCallOutcomeStatus(StrEnum):
    """Status of a retained model call result."""

    USABLE = "usable"
    INVALID = "invalid"
    UNUSABLE = "unusable"


class InvestigationStopReason(StrEnum):
    """Why an investigation execution stopped."""

    COMPLETED = "completed"
    PARTIAL_COVERAGE = "partial_coverage"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    SPENDING_BUDGET_EXHAUSTED = "spending_budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    MODEL_OUTCOME_UNKNOWN = "model_outcome_unknown"
    CANCELLED = "cancelled"
    ACCESS_REVOKED = "access_revoked"
    CONFIGURATION_MISSING = "configuration_missing"
    INTEGRITY_FAILURE = "integrity_failure"
    NONRESUMABLE = "nonresumable"


# ---------------------------------------------------------------------------
# Scope and specification
# ---------------------------------------------------------------------------


class InvestigationScopeV1(Contract):
    """Immutable scoped identity for an investigation execution."""

    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID
    investigation_id: UUID
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding_id: UUID | None = None
    finding_run_id: UUID | None = None
    chunk_set_id: UUID | None = None
    execution_revision: int = Field(default=1, ge=1)


class InvestigationLimitsV1(Contract):
    """Server-enforced execution ceilings."""

    max_steps: int = Field(default=DEFAULT_STEPS, ge=1, le=MAX_STEPS)
    max_model_calls_in_flight: int = Field(default=1, ge=1, le=1)
    max_tool_calls_in_flight: int = Field(default=1, ge=1, le=1)
    max_tool_calls_per_response: int = Field(
        default=MAX_TOOL_CALLS_PER_RESPONSE, ge=1, le=MAX_TOOL_CALLS_PER_RESPONSE
    )
    max_tool_calls_total: int = Field(
        default=MAX_TOOL_CALLS_PER_INVESTIGATION,
        ge=1,
        le=MAX_TOOL_CALLS_PER_INVESTIGATION,
    )
    max_search_hits: int = Field(default=DEFAULT_SEARCH_HITS, ge=1, le=MAX_SEARCH_HITS)
    max_memory_hits: int = Field(default=DEFAULT_MEMORY_HITS, ge=1, le=MAX_MEMORY_HITS)
    max_source_read_lines: int = Field(
        default=MAX_SOURCE_READ_LINES, ge=1, le=MAX_SOURCE_READ_LINES
    )
    max_context_tokens: int = Field(default=DEFAULT_CONTEXT_TOKENS, ge=1, le=MAX_CONTEXT_TOKENS)
    max_context_bytes: int = Field(default=DEFAULT_CONTEXT_BYTES, ge=1, le=MAX_CONTEXT_BYTES)
    budget_limit_micros: int = Field(default=5_000_000, gt=0)
    deadline_seconds: int = Field(
        default=INVESTIGATION_DEADLINE_SECONDS, ge=60, le=INVESTIGATION_DEADLINE_SECONDS
    )


class InvestigationSpecV1(Contract):
    """Frozen immutable specification for an investigation.

    Canonical JSON SHA-256 defines spec_hash. Once created, a spec
    is never mutated; a changed requirement produces a new investigation.
    """

    # Identity
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID

    # Target
    target_type: Literal["repository", "finding"]
    finding_id: UUID | None = None
    finding_run_id: UUID | None = None
    chunk_set_id: UUID | None = None
    chunk_config_id: UUID | None = None
    materialization_id: UUID | None = None

    # Query
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)

    # Mode
    mode: Literal["short_path", "two_specialist"]
    depth: Literal["quick", "standard", "deep"] = "standard"

    # Limits
    limits: InvestigationLimitsV1 = Field(default_factory=InvestigationLimitsV1)

    # Policy and route revisions (digests of configuration at create time)
    disclosure_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_policy_revision: str = Field(min_length=1, max_length=128)
    model_route_revision: str = Field(min_length=1, max_length=128)
    model_profile_revision: str = Field(min_length=1, max_length=128)
    runtime_revision: int = Field(default=INVESTIGATION_RUNTIME_REVISION, ge=1)
    privacy_policy_revision: str = Field(min_length=1, max_length=128)

    @field_validator("query")
    @classmethod
    def _query_bytes(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_QUERY_BYTES:
            raise ValueError(f"query exceeds {MAX_QUERY_BYTES} byte limit")
        return v

    @model_validator(mode="after")
    def _validate_target(self) -> Self:
        if self.target_type == "finding" and self.finding_id is None:
            raise ValueError("finding target requires finding_id")
        if self.target_type == "finding" and self.finding_run_id is None:
            raise ValueError("finding target requires finding_run_id")
        if self.target_type == "repository" and self.finding_id is not None:
            raise ValueError("repository target cannot have finding_id")
        return self

    @model_validator(mode="after")
    def _bounded_size(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > MAX_SPEC_BYTES:
            raise ValueError(f"spec exceeds {MAX_SPEC_BYTES} byte bound")
        return self

    def compute_spec_hash(self) -> str:
        """SHA-256 of canonical JSON representation."""
        return hashlib.sha256(canonical_bytes(self.model_dump())).hexdigest()


# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------


def compute_node_id(
    org_id: UUID,
    investigation_id: UUID,
    spec_hash: str,
    node_kind: InvestigationNodeKind | str,
    role: str,
    logical_ordinal: int,
) -> UUID:
    """Deterministic UUID5 for a logical investigation node.

    Stable across job attempts and reviewed resumes. Changes only
    with a new investigation/specification.
    """
    name = f"{org_id}:{investigation_id}:{spec_hash}:{node_kind}:{role}:{logical_ordinal}"
    return uuid5(_INVESTIGATION_NS, name)


def compute_call_id(node_id: UUID, call_ordinal: int) -> UUID:
    """Deterministic UUID5 for a model/tool call within a node."""
    return uuid5(_INVESTIGATION_NS, f"{node_id}:call:{call_ordinal}")


# ---------------------------------------------------------------------------
# Execution context (server-created only)
# ---------------------------------------------------------------------------


class InvestigationExecutionContextV1(Contract):
    """Passed to executor/tool ports. Never constructed from client input."""

    scope: InvestigationScopeV1
    logical_node_id: UUID
    node_kind: InvestigationNodeKind
    role: str = Field(min_length=1, max_length=64)
    logical_ordinal: int = Field(ge=0)
    attempt_job_id: UUID
    attempt_id: UUID
    attempt_fence: int = Field(gt=0)
    deadline: datetime
    limits: InvestigationLimitsV1


# ---------------------------------------------------------------------------
# Tool request / result
# ---------------------------------------------------------------------------


class InvestigationToolRequestV1(Contract):
    """Validated tool invocation — no actor/fence/provider/path-root overrides."""

    tool_name: InvestigationToolName
    arguments: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded_args(self) -> Self:
        if len(canonical_bytes(self.arguments)) > MAX_TOOL_ARG_BYTES:
            raise ValueError(f"tool arguments exceed {MAX_TOOL_ARG_BYTES} bytes")
        # Reject boolean coercion as integers
        for v in self.arguments.values():
            if isinstance(v, bool):
                # booleans are allowed as explicit bool type
                continue
            if isinstance(v, int) and isinstance(v, bool):
                raise ValueError("boolean passed as integer")
        return self


class InvestigationToolResultV1(Contract):
    """Bounded safe result from a tool invocation."""

    status: InvestigationToolStatus
    payload: str = Field(default="", max_length=MAX_TOOL_PAYLOAD_BYTES)
    evidence_refs: tuple[UUID, ...] = ()
    coverage_notes: str = Field(default="", max_length=1024)
    omission_reason: str = Field(default="", max_length=1024)
    result_digest: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")


# ---------------------------------------------------------------------------
# Model call references
# ---------------------------------------------------------------------------


class ModelInvocationRefV1(Contract):
    """Scoped identity for a model invocation — used by ModelResultStorePort."""

    org_id: UUID
    repository_id: UUID
    principal_id: UUID
    investigation_id: UUID
    node_id: UUID
    reservation_id: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Advance result
# ---------------------------------------------------------------------------


class InvestigationAdvanceV1(Contract):
    """Result of InvestigationExecutorPort.advance()."""

    status: InvestigationAdvanceStatus
    node_id: UUID | None = None
    checkpoint_sequence: int | None = None
    stop_reason: InvestigationStopReason | None = None
    message: str = Field(default="", max_length=1024)
    draft: InvestigationReportDraftV1 | None = None


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


class InvestigationAuthorityV1(Contract):
    """Current authority state for an investigation job."""

    db_now: datetime
    deadline: datetime
    is_cancelled: bool = False
    is_expired: bool = False
    scope: InvestigationScopeV1


# ---------------------------------------------------------------------------
# Claims and evidence
# ---------------------------------------------------------------------------


class InvestigationClaimV2(Contract):
    """Typed claim with evidence binding for report grounding."""

    claim_id: UUID
    statement: str = Field(min_length=1, max_length=MAX_CLAIM_STATEMENT_CHARS)
    kind: InvestigationClaimKind
    evidence_ids: tuple[UUID, ...] = ()
    suggested_file: str | None = Field(default=None, max_length=512)
    suggested_line_start: int | None = Field(default=None, ge=1)
    suggested_line_end: int | None = Field(default=None, ge=1)
    suggested_quote: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _line_order(self) -> Self:
        if (
            self.suggested_line_start is not None
            and self.suggested_line_end is not None
            and self.suggested_line_start > self.suggested_line_end
        ):
            raise ValueError("line_start must be <= line_end")
        return self


class ClaimVerificationStatus(StrEnum):
    """Verification outcome for a claim."""

    SOURCE_CORRESPONDENCE_CONFIRMED = "source_correspondence_confirmed"
    UNSUPPORTED = "unsupported"
    REFUTED = "refuted"
    LOCATION_VALID_QUOTE_MISSING = "location_valid_quote_missing"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


class ClaimVerificationV1(Contract):
    """Server-side verification result for a single claim."""

    claim_id: UUID
    status: ClaimVerificationStatus
    resolved_file: str | None = None
    resolved_line_start: int | None = None
    resolved_line_end: int | None = None
    source_digest: str | None = Field(default=None, pattern=r"^([0-9a-f]{64})?$")
    notes: str = Field(default="", max_length=1024)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class InvestigationReportV1(Contract):
    """Immutable final investigation report."""

    report_id: UUID
    investigation_id: UUID
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_revision: int = Field(ge=1)

    verdict: str = Field(max_length=64)
    verdict_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    summary: str = Field(max_length=8192)

    claims: tuple[InvestigationClaimV2, ...] = ()
    verifications: tuple[ClaimVerificationV1, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()

    coverage_notes: str = Field(default="", max_length=4096)
    unknowns: str = Field(default="", max_length=4096)
    limitations: str = Field(default="", max_length=4096)

    settled_usage_micros: int = Field(default=0, ge=0)
    held_usage_micros: int = Field(default=0, ge=0)

    stop_reason: InvestigationStopReason
    current_support_available: bool = True
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if len(self.claims) > MAX_CLAIMS:
            raise ValueError(f"claims exceed {MAX_CLAIMS} limit")
        if len(self.evidence_refs) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence refs exceed {MAX_EVIDENCE_REFS} limit")
        return self

    def compute_report_digest(self) -> str:
        d = self.model_dump(mode="json", exclude={"report_digest"})
        return hashlib.sha256(canonical_bytes(d)).hexdigest()


class InvestigationReportDraftV1(Contract):
    """Prepared report draft for atomic publication."""

    investigation_id: UUID
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: str = Field(max_length=64)
    verdict_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    summary: str = Field(max_length=8192)
    claims: tuple[InvestigationClaimV2, ...] = ()
    verifications: tuple[ClaimVerificationV1, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()
    coverage_notes: str = Field(default="", max_length=4096)
    unknowns: str = Field(default="", max_length=4096)
    limitations: str = Field(default="", max_length=4096)
    settled_usage_micros: int = Field(default=0, ge=0)
    held_usage_micros: int = Field(default=0, ge=0)
    stop_reason: InvestigationStopReason
    current_support_available: bool = True
    draft_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if len(self.claims) > MAX_CLAIMS:
            raise ValueError(f"claims exceed {MAX_CLAIMS} limit")
        if len(self.evidence_refs) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence refs exceed {MAX_EVIDENCE_REFS} limit")
        return self

    def compute_draft_digest(self) -> str:
        d = self.model_dump(mode="json", exclude={"draft_digest"})
        return hashlib.sha256(canonical_bytes(d)).hexdigest()


class InvestigationPublicationReceiptV1(Contract):
    """Immutable receipt binding a report to its authoritative job/attempt."""

    receipt_id: UUID
    investigation_id: UUID
    job_id: UUID
    attempt_id: UUID
    attempt_fence: int = Field(gt=0)
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_id: UUID
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


# ---------------------------------------------------------------------------
# Execution state and checkpoints
# ---------------------------------------------------------------------------


class InvestigationExecutionStateV1(Contract):
    """Bounded durable execution state for checkpointing.

    Does not contain repeated complete model/tool blobs or decrypted
    signed reasoning. Required messages are reconstructed from retained
    records by the executor.
    """

    investigation_id: UUID | None = None
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_revision: int = Field(ge=1)

    # Cursor
    next_node_kind: InvestigationNodeKind
    next_role: str = Field(min_length=1, max_length=64)
    next_ordinal: int = Field(ge=0)
    checkpoint_sequence: int = Field(ge=0)

    # Committed references
    committed_model_receipt_ids: tuple[UUID, ...] = ()
    committed_tool_receipt_ids: tuple[UUID, ...] = ()
    committed_evidence_ids: tuple[UUID, ...] = ()

    # Context provenance
    context_packet_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    memory_provenance_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")

    # Claim/report draft references
    claim_ids: tuple[UUID, ...] = ()
    report_draft_digest: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")

    # Stop tracking
    stop_reason: InvestigationStopReason | None = None
    usage_projection_micros: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _bounded_size(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > MAX_CHECKPOINT_BYTES:
            raise ValueError(f"execution state exceeds {MAX_CHECKPOINT_BYTES} bytes")
        return self

    def compute_state_digest(self) -> str:
        return hashlib.sha256(canonical_bytes(self.model_dump())).hexdigest()


class InvestigationExecutionCheckpointV1(Contract):
    """Immutable checkpoint record."""

    org_id: UUID
    investigation_id: UUID
    execution_revision: int = Field(ge=1)
    checkpoint_sequence: int = Field(ge=0)
    state: InvestigationExecutionStateV1
    state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    writer_job_id: UUID
    writer_attempt_id: UUID
    writer_fence: int = Field(gt=0)
    created_at: datetime


class InvestigationEvidenceV1(Contract):
    """Immutable tool/source observation record."""

    id: UUID
    org_id: UUID
    investigation_id: UUID
    snapshot_id: UUID
    source_path: str = Field(min_length=1, max_length=1024)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_start_line: int = Field(ge=1)
    original_end_line: int = Field(ge=1)
    safe_start_line: int = Field(ge=1)
    safe_end_line: int = Field(ge=1)
    excerpt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_type: str = Field(min_length=1, max_length=64)
    coverage_summary: str = Field(default="", max_length=4096)
    limitations: str = Field(default="", max_length=4096)
    tool_receipt_id: UUID | None = None
    created_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_lines(self) -> Self:
        if self.original_start_line > self.original_end_line:
            raise ValueError("original_start_line must be <= original_end_line")
        if self.safe_start_line > self.safe_end_line:
            raise ValueError("safe_start_line must be <= safe_end_line")
        return self

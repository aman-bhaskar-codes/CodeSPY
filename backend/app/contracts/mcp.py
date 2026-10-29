"""Versioned, frozen contracts and input/result schemas for read-only MCP tools.

Enforces strict bounds, provenance retention, and negative identity isolation
(tool arguments can never specify or override org_id, principal_id, or role).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class McpContract(BaseModel):
    """Base model for MCP contracts enforcing immutable, strictly forbidden extras."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def reject_identity_injection(cls, values: Any) -> Any:
        if isinstance(values, dict):
            forbidden = {"org_id", "principal_id", "role", "user_id", "credentials", "token"}
            found = forbidden.intersection(values)
            if found:
                raise ValueError(
                    f"Tool arguments cannot supply identity or credential fields: {sorted(found)}"
                )
        return values


# ---------------------------------------------------------------------------
# Tool Input DTOs (13 tools)
# ---------------------------------------------------------------------------


class GetIndexStatusInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    job_id: UUID


class GetSourceViewInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    file_id: UUID
    start_byte: int | None = Field(default=None, ge=0)
    end_byte: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_endpoints(self) -> GetSourceViewInputV1:
        if (self.start_byte is not None) != (self.end_byte is not None):
            raise ValueError("Byte range requires both start_byte and end_byte")
        if (
            self.start_byte is not None
            and self.end_byte is not None
            and self.start_byte > self.end_byte
        ):
            raise ValueError("start_byte cannot exceed end_byte")
        return self


class SearchExactInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    query: str = Field(min_length=1, max_length=1024)
    chunk_set_id: UUID | None = None
    limit: int = Field(default=5, ge=1, le=10)


class SearchHybridInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    query: str = Field(min_length=1, max_length=1024)
    chunk_set_id: UUID | None = None
    limit: int = Field(default=5, ge=1, le=10)


class AssembleContextInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    chunk_set_id: UUID | None = None
    max_tokens: int = Field(default=4096, ge=128, le=32768)


class QueryMemoryInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    query: str = Field(min_length=1, max_length=1024)
    topic: str | None = Field(default=None, max_length=256)
    history_mode: Literal["off", "current_only", "historical_hints"] = "current_only"
    limit: int = Field(default=5, ge=1, le=10)


class GetInvestigationStatusInputV1(McpContract):
    repository_id: UUID
    investigation_id: UUID


class GetInvestigationResultInputV1(McpContract):
    repository_id: UUID
    investigation_id: UUID


class ListFindingsInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    run_id: UUID
    category: str | None = Field(default=None, max_length=128)
    severity: str | None = Field(default=None, max_length=32)
    disposition: str | None = Field(default=None, max_length=64)
    triage_state: str | None = Field(default=None, max_length=64)
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=20, ge=1, le=50)


class GetEvidenceInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    evidence_id: UUID
    run_id: UUID | None = None


class GraphNeighborsInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    node_id: UUID
    structural_run_id: UUID | None = None
    max_hops: int = Field(default=1, ge=1, le=2)
    max_nodes: int = Field(default=20, ge=1, le=50)
    max_edges: int = Field(default=40, ge=0, le=100)


class TraceFeatureInputV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    finding_id: UUID
    max_hops: int = Field(default=2, ge=0, le=2)
    max_nodes: int = Field(default=50, ge=1, le=100)


class ListClaimsInputV1(McpContract):
    repository_id: UUID
    investigation_id: UUID


class McpInvestigationStatusV1(McpContract):
    current_support_available: bool = True
    investigation_id: UUID
    repository_id: UUID
    snapshot_id: UUID | None
    status: str
    target_type: str
    query: str
    current_step: int
    max_steps: int
    budget_limit_micros: int
    spent_micros: int
    held_micros: int
    verdict: str | None
    verdict_confidence: float | None
    summary: str | None
    failure_reason: str | None
    available_actions: list[str]
    claims_count: int
    evidence_count: int
    events_count: int
    job: dict[str, Any] | None


class McpInvestigationResultV1(McpContract):
    snapshot_id: UUID | None = None
    current_support_available: bool = True
    investigation_id: UUID
    repository_id: UUID
    status: str
    verdict: str | None
    verdict_confidence: float | None
    summary: str | None
    claims_count: int
    evidence_count: int
    report: dict[str, Any] | None


class McpErrorV1(McpContract):
    code: Literal[
        "response_too_large",
        "capacity_exceeded",
        "timeout",
        "scope_denied",
        "invalid_parameters",
        "capability_unavailable",
    ]
    message: str | None = None


# ---------------------------------------------------------------------------
# Output / Result DTOs
# ---------------------------------------------------------------------------


class McpClaimItemV1(McpContract):
    claim_id: UUID
    statement: str
    kind: str
    status: str
    evidence_refs: tuple[UUID, ...] = ()
    resolved_file: str | None = None
    resolved_line_start: int | None = None
    resolved_line_end: int | None = None
    notes: str = ""


class ListClaimsResultV1(McpContract):
    snapshot_id: UUID | None = None
    current_support_available: bool = True
    repository_id: UUID
    investigation_id: UUID
    claims: tuple[McpClaimItemV1, ...] = ()
    total_count: int


class McpEvidenceResultV1(McpContract):
    provider_name: str | None = None
    provider_version: str | None = None
    operation: str | None = None
    evidence_digest: str | None = None
    source_support: Literal["not_revalidated"] = "not_revalidated"
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    run_id: UUID
    file_id: UUID | None = None
    file_path: str | None = None
    range: dict[str, Any] | None = None
    observation_mode: str
    basis: str
    coverage_status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    captured_at: str | None = None


class McpFindingItemV1(McpContract):
    id: UUID
    rule_id: str
    category: str
    severity: str
    disposition: str
    current_support: str
    title: str
    file_path: str | None = None
    line: int | None = None
    triage_state: str | None = None


class ListFindingsResultV1(McpContract):
    repository_id: UUID
    snapshot_id: UUID
    run_id: UUID
    findings: tuple[McpFindingItemV1, ...] = ()
    next_cursor: str | None = None
    total_count: int


class McpGraphNodeV1(McpContract):
    file_id: UUID | None = None
    id: UUID
    kind: str
    label: str
    file_path: str | None = None
    line: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class McpGraphEdgeV1(McpContract):
    source_id: UUID
    target_id: UUID
    edge_kind: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphNeighborsResultV1(McpContract):
    coverage_status: Literal["partial"] = "partial"
    repository_id: UUID
    snapshot_id: UUID
    structural_run_id: UUID
    root_node_id: UUID
    nodes: tuple[McpGraphNodeV1, ...] = ()
    edges: tuple[McpGraphEdgeV1, ...] = ()
    hop_count: int
    omission_reasons: tuple[str, ...] = ()

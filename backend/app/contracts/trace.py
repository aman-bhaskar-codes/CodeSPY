"""Feature trace contracts for code graph neighborhood exploration."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TraceNodeV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    label: str
    file_path: str | None = None
    line: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class TraceEdgeV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: UUID
    target_id: UUID
    edge_kind: str
    properties: dict[str, Any] = Field(default_factory=dict)


class TraceBudgetV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_hops: int = Field(default=2, ge=0, le=2)
    max_nodes: int = Field(default=100, ge=1, le=100)
    max_edges: int = Field(default=200, ge=0, le=200)
    timeout_seconds: float = Field(default=1.0, gt=0, le=1.0)


class FeatureTraceV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: UUID
    structural_run_id: UUID | None = None
    omission_reasons: list[str] = Field(default_factory=list)
    nodes: list[TraceNodeV1] = Field(default_factory=list)
    edges: list[TraceEdgeV1] = Field(default_factory=list)
    hop_count: int = 0
    budget_exceeded: bool = False

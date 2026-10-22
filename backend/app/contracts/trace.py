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
    model_config = ConfigDict(frozen=True)

    max_hops: int = 2
    max_nodes: int = 100
    max_edges: int = 200
    timeout_seconds: float = 1.0


class FeatureTraceV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: UUID
    nodes: list[TraceNodeV1] = Field(default_factory=list)
    edges: list[TraceEdgeV1] = Field(default_factory=list)
    hop_count: int = 0
    budget_exceeded: bool = False

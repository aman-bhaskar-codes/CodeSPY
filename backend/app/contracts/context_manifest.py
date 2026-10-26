"""Reference-only request provenance; no raw prompt or provider continuation payloads."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.search import SearchScopeV1


class MemoryReferenceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: UUID
    version: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_hint: bool = False
    source_snapshot_id: UUID
    source_correspondence: Literal[
        "original_snapshot", "matching_files", "changed", "human_note"
    ] = "original_snapshot"


class RequestContextLimitsV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_input_tokens: int = Field(default=4096, ge=128, le=8192)
    max_wire_bytes: int = Field(default=32768, ge=512, le=65536)
    safety_tokens: int = Field(default=256, ge=0, le=1024)


class ContextBudgetBreakdownV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int
    wire_bytes: int
    system_tokens: int
    tool_schema_tokens: int
    required_history_tokens: int
    source_tokens: int
    memory_tokens: int
    output_reserve_tokens: int
    safety_tokens: int
    measurement: Literal["cl100k_estimate"] = "cl100k_estimate"


class ContextManifestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1
    investigation_id: UUID
    logical_call_id: UUID
    strategy_revision: Literal["reviewed-context-v1"] = "reviewed-context-v1"
    profile_digest: str
    policy_digest: str
    selection_digest: str
    prepared_request_digest: str
    required_history_digest: str
    memory_references: tuple[MemoryReferenceV1, ...] = Field(max_length=10)
    source_packet_digest: str | None = None
    omissions: tuple[str, ...]
    limits: RequestContextLimitsV1
    budget: ContextBudgetBreakdownV1


class MemoryContextSettingsV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["disabled", "shadow", "reviewed"] = "disabled"
    history_mode: Literal["current_only", "historical_hints"] = "historical_hints"
    limits: RequestContextLimitsV1 = Field(default_factory=RequestContextLimitsV1)

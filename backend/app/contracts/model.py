"""Domain contracts for vendor-neutral model gateway, routing, capabilities, and accounting."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelProvider(StrEnum):
    FAKE = "fake"
    OPENAI = "openai"
    BEDROCK_CLAUDE = "bedrock_claude"
    GOOGLE_GEMINI = "google_gemini"


class ModelRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolDefinitionV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1, max_length=1024)
    parameters_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCallV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    arguments_json: str = Field(default="{}")


class ToolResultV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=64)
    content: str
    is_error: bool = False


class ChatMessageV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ModelRole
    content: str = ""
    name: str | None = None
    tool_calls: tuple[ToolCallV1, ...] = ()
    tool_call_id: str | None = None
    continuation_ref: UUID | None = None


class ModelCapabilityV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ModelProvider
    model_id: str = Field(min_length=1, max_length=128)
    revision: str = "2026-10-22"
    capability_type: Literal["generation", "embedding"] = "generation"
    max_context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    supports_tools: bool = False
    supports_structured_output: bool = False
    supports_streaming: bool = True
    supports_thinking: bool = False
    geographic_regions: tuple[str, ...] = ("us-east-1",)
    requires_disclosure_approval: bool = True
    default_price_schedule_version: str = "2026-10-22.1"


class GenerationPriceScheduleV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_id: str
    version: str
    input_micros_per_1k: int = Field(ge=0)
    output_micros_per_1k: int = Field(ge=0)
    thinking_micros_per_1k: int = Field(default=0, ge=0)
    cache_read_micros_per_1k: int = Field(default=0, ge=0)
    cache_write_micros_per_1k: int = Field(default=0, ge=0)
    effective_at: datetime
    expires_at: datetime

    def is_active(self, at: datetime | None = None) -> bool:
        ts = at or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        eff = (
            self.effective_at
            if self.effective_at.tzinfo is not None
            else self.effective_at.replace(tzinfo=UTC)
        )
        exp = (
            self.expires_at
            if self.expires_at.tzinfo is not None
            else self.expires_at.replace(tzinfo=UTC)
        )
        return eff <= ts <= exp

    def calculate_cost_micros(
        self,
        input_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> int:
        def ceil_micros(count: int, rate: int) -> int:
            if count <= 0 or rate <= 0:
                return 0
            return (count * rate + 999) // 1000

        return (
            ceil_micros(input_tokens, self.input_micros_per_1k)
            + ceil_micros(output_tokens, self.output_micros_per_1k)
            + ceil_micros(thinking_tokens, self.thinking_micros_per_1k)
            + ceil_micros(cache_read_tokens, self.cache_read_micros_per_1k)
            + ceil_micros(cache_write_tokens, self.cache_write_micros_per_1k)
        )


class TokenUsageV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    thinking_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.thinking_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class ModelRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    investigation_id: UUID
    call_id: UUID
    attempt_id: int = Field(ge=1)
    provider: str
    model_id: str
    messages: tuple[ChatMessageV1, ...]
    tools: tuple[ToolDefinitionV1, ...] = ()
    tool_choice: Literal["auto", "none", "required"] | str = "auto"
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=2048, gt=0, le=32768)
    response_schema: dict[str, Any] | None = None
    context_manifest_id: UUID | None = None
    reservation_id: UUID
    opaque_context_json: str = Field(default="{}", repr=False, max_length=1048576)


class ModelResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: UUID
    attempt_id: int
    provider: str
    model_id: str
    content: str
    tool_calls: tuple[ToolCallV1, ...] = ()
    usage: TokenUsageV1
    charge_micros: int = Field(ge=0)
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter", "error"] = "stop"
    provider_request_id: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    continuation_ref: UUID | None = None
    context_packet_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    memory_provenance_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def paired_context_references(self) -> "ModelResponseV1":
        if (self.context_packet_hash is None) != (self.memory_provenance_hash is None):
            raise ValueError("context references must be paired")
        return self


class ModelStreamChunkV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: UUID
    delta_text: str = ""
    tool_call_delta: ToolCallV1 | None = None
    is_final: bool = False
    usage: TokenUsageV1 | None = None
    finish_reason: str | None = None


class ModelRoutingRequirementV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: Literal["short_path", "planner", "auditor", "verifier", "synthesizer"] = "short_path"
    estimated_context_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(default=1024, gt=0)
    requires_tools: bool = False
    requires_structured_output: bool = False
    required_region: str | None = None


class RoutedModelV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_id: str
    price_schedule_version: str
    reservation_id: UUID
    estimated_micros: int = Field(ge=0)
    is_fallback: bool = False
    model_alias: str | None = None
    profile_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

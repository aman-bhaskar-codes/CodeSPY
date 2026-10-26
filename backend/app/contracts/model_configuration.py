"""Non-secret, immutable model profiles and explicit task routing."""

from typing import Literal

from pydantic import Field, model_validator

from app.contracts.model import ModelProvider
from app.domain.scope import Contract

ModelSlot = Literal[
    "openai_fast", "openai_reasoning", "claude_fast", "claude_reasoning", "gemini_general"
]
ModelTask = Literal["short_path", "planner", "auditor", "verifier", "synthesizer"]
EndpointKind = Literal["openai_responses", "bedrock_converse", "gemini_generate_content"]


class ModelTimeoutsV1(Contract):
    connect_seconds: float = Field(default=5.0, gt=0, le=120)
    write_seconds: float = Field(default=15.0, gt=0, le=120)
    pool_seconds: float = Field(default=2.0, gt=0, le=120)
    read_seconds: float = Field(default=60.0, gt=0, le=120)
    total_seconds: float = Field(default=120.0, gt=0, le=120)


class ModelProfileV1(Contract):
    alias: ModelSlot
    provider: ModelProvider
    model_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    profile_revision: str = Field(min_length=1, max_length=64)
    enabled: bool = False
    endpoint_kind: EndpointKind
    api_version: str | None = Field(default=None, min_length=1, max_length=32)
    source_region: str | None = None
    processing_regions: tuple[str, ...] = Field(default=(), max_length=32)
    credential_ref: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    max_context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0, le=32768)
    price_version: str = Field(min_length=1, max_length=32)
    normalization_version: Literal["usage-v1"] = "usage-v1"
    locally_qualified: bool = False
    owner_approved_live: bool = False
    input_bound_qualified: bool = False
    allowed_task_types: tuple[ModelTask, ...] = Field(min_length=1, max_length=5)
    supports_tools: bool = False
    supports_native_schema: bool = False
    supports_streaming: bool = False
    reasoning_mode: Literal["none", "provider_default", "bounded"] = "none"
    thinking_token_budget: int = Field(default=0, ge=0, le=32768)
    temperature: float | None = Field(default=None, ge=0, le=1)
    cache_policy: Literal["implicit_only", "disabled"] = "disabled"
    request_byte_limit: int = Field(default=1048576, gt=0, le=1048576)
    response_byte_limit: int = Field(default=2097152, gt=0, le=2097152)
    tool_call_limit: int = Field(default=16, ge=0, le=16)
    tool_argument_byte_limit: int = Field(default=65536, gt=0, le=65536)
    timeouts: ModelTimeoutsV1 = Field(default_factory=ModelTimeoutsV1)

    @model_validator(mode="after")
    def validate_provider_binding(self) -> "ModelProfileV1":
        expected = {
            "openai": ("openai_", "openai_responses"),
            "bedrock_claude": ("claude_", "bedrock_converse"),
            "google_gemini": ("gemini_", "gemini_generate_content"),
        }.get(self.provider.value)
        if (
            expected is None
            or not self.alias.startswith(expected[0])
            or self.endpoint_kind != expected[1]
        ):
            raise ValueError("model slot and endpoint must match provider")
        if self.provider == ModelProvider.BEDROCK_CLAUDE:
            if self.source_region != "ap-south-1":
                raise ValueError("Bedrock source region must be explicitly ap-south-1")
        elif self.source_region is not None:
            raise ValueError("source region applies only to Bedrock")
        if self.reasoning_mode != "bounded" and self.thinking_token_budget:
            raise ValueError("thinking budget requires bounded reasoning mode")
        if self.reasoning_mode == "bounded" and not self.thinking_token_budget:
            raise ValueError("bounded reasoning requires a positive budget")
        if len(set(self.allowed_task_types)) != len(self.allowed_task_types):
            raise ValueError("duplicate allowed task type")
        if any(not region.strip() for region in self.processing_regions):
            raise ValueError("processing regions must be nonempty")
        return self


class ModelTaskRouteV1(Contract):
    task_type: ModelTask
    aliases: tuple[ModelSlot, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_aliases(self) -> "ModelTaskRouteV1":
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("duplicate task route alias")
        return self


class ModelRegistryConfigV1(Contract):
    profiles: tuple[ModelProfileV1, ...] = Field(default=(), max_length=5)
    task_routes: tuple[ModelTaskRouteV1, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def validate_registry(self) -> "ModelRegistryConfigV1":
        aliases = [profile.alias for profile in self.profiles]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate profile alias")
        enabled = [(p.provider, p.model_id) for p in self.profiles if p.enabled]
        if len(enabled) != len(set(enabled)):
            raise ValueError("enabled slots require distinct provider model IDs")
        tasks = [route.task_type for route in self.task_routes]
        if len(tasks) != len(set(tasks)):
            raise ValueError("duplicate task route")
        if any(alias not in aliases for route in self.task_routes for alias in route.aliases):
            raise ValueError("task route references unknown alias")
        return self


class ResolvedModelProfileV1(Contract):
    profile: ModelProfileV1
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

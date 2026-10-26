"""Bounded provider evidence independent of prompts, credentials and spend ledgers."""

from typing import Literal

from pydantic import Field, model_validator

from app.contracts.model import ModelRequestV1, ModelResponseV1, TokenUsageV1
from app.contracts.model_configuration import ModelProfileV1
from app.domain.scope import Contract

UsageCategory = Literal[
    "input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "cache_write_tokens"
]


class ProviderUsageEvidenceV1(Contract):
    normalization_version: Literal["usage-v1"] = "usage-v1"
    quality: Literal["measured", "aggregate_output", "unknown"]
    usage: TokenUsageV1 | None = None
    unavailable_breakdowns: tuple[UsageCategory, ...] = ()
    not_applicable: tuple[UsageCategory, ...] = ()
    raw_counters_json: str = Field(default="{}", max_length=16384)
    reason: Literal["complete", "missing_or_inconsistent_usage", "unsupported_usage"] = "complete"

    @model_validator(mode="after")
    def consistent_quality(self) -> "ProviderUsageEvidenceV1":
        if (self.quality == "unknown") != (self.usage is None):
            raise ValueError("usage must match evidence quality")
        if self.quality == "aggregate_output" and self.unavailable_breakdowns != (
            "thinking_tokens",
        ):
            raise ValueError("aggregate output requires explicit unavailable thinking breakdown")
        return self


class PreparedModelCallV1(Contract):
    profile: "ModelProfileV1"
    request: "ModelRequestV1" = Field(repr=False)
    body_json: str = Field(max_length=1048576, repr=False)
    wire_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProviderOutcomeV1(Contract):
    response: "ModelResponseV1 | None" = Field(default=None, repr=False)
    usage: ProviderUsageEvidenceV1
    continuation_json: str | None = Field(default=None, repr=False, max_length=262144)
    failure_code: Literal["missing_usage", "unsupported_protocol", "invalid_output"] | None = None

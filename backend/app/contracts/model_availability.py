"""Sanitized model availability; no credentials, account identifiers or probe claims."""

from typing import Literal

from pydantic import Field

from app.contracts.model_configuration import ModelSlot
from app.domain.scope import Contract

ModelAvailabilityStatus = Literal[
    "disabled", "unavailable", "locally_qualified", "live_unqualified"
]


class ModelAvailabilityItemV1(Contract):
    alias: ModelSlot
    provider: str
    enabled: bool
    locally_qualified: bool
    owner_approved_live: bool
    eligible: bool
    status: ModelAvailabilityStatus
    supports_tools: bool
    supports_native_schema: bool
    supports_streaming: bool = False


class ModelAvailabilityV1(Contract):
    profiles: tuple[ModelAvailabilityItemV1, ...] = Field(default=(), max_length=5)
    live_access_verified: bool = False

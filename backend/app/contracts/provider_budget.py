"""Versioned contracts for provider disclosure policies, monetary units, and budget reservations."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def calculate_charge_micros(token_count: int, rate_micros_per_1k: int) -> int:
    """Calculate integer microdollars for a given token count and rate per 1,000 tokens.

    Uses integer arithmetic with ceiling division to avoid floating point imprecision
    and guarantee that fractional microdollars are safely accounted for.
    """
    if token_count < 0:
        raise ValueError("tokens cannot be negative")
    if rate_micros_per_1k < 0:
        raise ValueError("rate cannot be negative")
    if token_count == 0 or rate_micros_per_1k == 0:
        return 0
    return (token_count * rate_micros_per_1k + 999) // 1000


class ReservationState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    UNCERTAIN_HOLD = "uncertain_hold"


class DisclosurePolicyV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_external_disclosure: bool = False
    allowed_providers: tuple[str, ...] = ("fake",)
    allowed_regions: tuple[str, ...] = ("us-east-1",)
    policy_version: str = "2026-10-22.1"

    def is_provider_permitted(self, provider: str) -> bool:
        if not self.allow_external_disclosure and provider != "fake":
            return False
        return provider in self.allowed_providers

    def is_region_permitted(self, region: str) -> bool:
        return region in self.allowed_regions


class PriceScheduleV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_id: str
    version: str
    input_micros_per_1k: int = Field(gt=0)
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


class ModelCapabilityV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_id: str
    revision: str
    capability: Literal["embedding", "generation"]
    max_tokens: int = Field(gt=0)
    dimensions: int | None = None
    supports_normalization: bool = True
    default_price_schedule_version: str


class BudgetReservationRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: UUID
    repository_id: UUID
    principal_id: UUID
    provider: str
    model_id: str
    estimated_tokens: int = Field(gt=0, le=2147483647, strict=True)
    max_charge_micros: int = Field(gt=0, le=9223372036854775807, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128)


class ReservationRecordV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: UUID
    org_id: UUID
    repository_id: UUID
    principal_id: UUID
    provider: str
    model_id: str
    estimated_tokens: int
    estimated_micros: int
    settled_micros: int | None = None
    state: ReservationState
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None = None

    def is_active_hold(self) -> bool:
        return self.state in (ReservationState.RESERVED, ReservationState.UNCERTAIN_HOLD)

    def commit(
        self, actual_micros: int, timestamp: datetime | None = None
    ) -> "ReservationRecordV1":
        ts = timestamp or datetime.now(UTC)
        return self.model_copy(
            update={
                "state": ReservationState.COMMITTED,
                "settled_micros": actual_micros,
                "updated_at": ts,
            }
        )

    def release(
        self, timestamp: datetime | None = None, reason: str | None = None
    ) -> "ReservationRecordV1":
        ts = timestamp or datetime.now(UTC)
        return self.model_copy(
            update={
                "state": ReservationState.RELEASED,
                "updated_at": ts,
                "failure_reason": reason,
            }
        )

    def hold_uncertain(
        self, timestamp: datetime | None = None, reason: str | None = None
    ) -> "ReservationRecordV1":
        ts = timestamp or datetime.now(UTC)
        return self.model_copy(
            update={
                "state": ReservationState.UNCERTAIN_HOLD,
                "updated_at": ts,
                "failure_reason": reason or "timeout_during_dispatch",
            }
        )


class SettlementOutcomeV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: UUID
    status: Literal["success", "failed", "timeout", "duplicate_replay"]
    actual_tokens: int = Field(ge=0, le=9223372036854775807, strict=True)
    actual_charge_micros: int = Field(ge=0, le=9223372036854775807, strict=True)
    provider_request_id: str | None = None
    error_message: str | None = None


class EmbeddingRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    provider: str = "fake"
    model_id: str = "amazon.titan-embed-text-v2:0"
    dimensions: int = 1024
    normalize: bool = True


class EmbeddingResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vector: tuple[float, ...]
    token_count: int
    model_id: str
    provider: str
    provider_request_id: str | None = None

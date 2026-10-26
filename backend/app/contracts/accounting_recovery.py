"""Contracts for bounded, queue-independent accounting recovery worker and settings."""

from pydantic import Field, model_validator

from app.domain.scope import Contract


class AccountingRecoverySettingsV1(Contract):
    enabled: bool = False
    interval_seconds: float = Field(default=15.0, gt=0.0)
    batch_limit: int = Field(default=100, ge=1, le=100)
    stale_age_seconds: int = Field(default=300, ge=60)
    max_provider_call_duration_seconds: int | None = Field(default=None, gt=0)
    recovery_margin_seconds: int = Field(default=30, ge=0)

    @model_validator(mode="after")
    def validate_duration_bounds(self) -> "AccountingRecoverySettingsV1":
        if self.enabled:
            if self.max_provider_call_duration_seconds is None:
                raise ValueError("bounded max_provider_call_duration_seconds required when enabled")
            required_min = self.max_provider_call_duration_seconds + self.recovery_margin_seconds
            if self.stale_age_seconds <= required_min:
                raise ValueError(
                    f"stale_age_seconds ({self.stale_age_seconds}) must exceed "
                    f"max_provider_call_duration_seconds + margin ({required_min})"
                )
        return self


class AccountingRecoverySummaryV1(Contract):
    scanned: int = Field(default=0, ge=0)
    held: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    failed_scopes: int = Field(default=0, ge=0)
    continuation_pending: bool = False

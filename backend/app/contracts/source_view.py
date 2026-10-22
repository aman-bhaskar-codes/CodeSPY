"""Typed contracts for verified source views, privacy transformations, and offset mappings."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceRepresentation(StrEnum):
    ORIGINAL = "original"
    MODEL_SAFE = "model_safe"


class ByteRangeV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_byte: int = Field(default=0, ge=0)
    end_byte: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.start_byte > self.end_byte:
            raise ValueError(f"start_byte {self.start_byte} cannot exceed end_byte {self.end_byte}")
        return self

    @property
    def length(self) -> int:
        return self.end_byte - self.start_byte


class IntervalMappingV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    safe_start_byte: int = Field(ge=0)
    safe_end_byte: int = Field(ge=0)
    original_start_byte: int = Field(ge=0)
    original_end_byte: int = Field(ge=0)
    is_redacted: bool = False
    redaction_type: str | None = None

    @model_validator(mode="after")
    def validate_intervals(self) -> Self:
        if self.safe_start_byte > self.safe_end_byte:
            raise ValueError("safe_start_byte cannot exceed safe_end_byte")
        if self.original_start_byte > self.original_end_byte:
            raise ValueError("original_start_byte cannot exceed original_end_byte")
        return self


DEFAULT_SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*.key",
    "*.pkcs12",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*credentials*",
    "*service_account*.json",
    ".aws/*",
    ".ssh/*",
)

REMAINING_PRIVACY_RISK_LIMITATIONS: tuple[str, ...] = (
    "Heuristic secret detection: non-standard high-entropy strings, split tokens, "
    "or steganographic keys may remain unredacted.",
    "Surrounding semantic structure and identifier names may imply the class "
    "or purpose of redacted secrets.",
    "Model-safe representation is derived; downstream disclosure requires explicit "
    "provider policy authorization.",
)


class PrivacyPolicyV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_version: str = "v1_default"
    allow_original_view: bool = False
    sensitive_path_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATH_PATTERNS
    redact_secrets: bool = True
    replacement_template: str = "[REDACTED_SECRET:{secret_type}]"
    max_view_bytes: int = 1_048_576  # 1 MiB


class SourceViewRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID
    file_id: UUID
    byte_range: ByteRangeV1 | None = None
    representation: SourceRepresentation = SourceRepresentation.MODEL_SAFE


class SourceViewResultV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_id: UUID
    file_path: str
    file_sha256: str
    representation: SourceRepresentation
    requested_range: ByteRangeV1
    actual_range: ByteRangeV1
    content_bytes: bytes
    content_text: str | None = None
    content_sha256: str
    total_file_size: int
    policy_version: str | None = None
    redaction_map_digest: str | None = None
    interval_mappings: tuple[IntervalMappingV1, ...] = ()
    remaining_privacy_risks: tuple[str, ...] = ()
    is_omitted: bool = False
    omission_reason: str | None = None

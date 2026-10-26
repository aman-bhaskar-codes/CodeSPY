"""Typed contracts for evidence-backed repository memory."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryKind(StrEnum):
    TRIAGE_RESOLUTION = "triage_resolution"
    SECURITY_FINDING_EVIDENCE = "security_finding_evidence"
    ARCHITECTURE_FACT = "architecture_fact"
    INVESTIGATION_EPISODE = "investigation_episode"


class EvidencePointerV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    finding_id: UUID | None = None
    chunk_id: UUID | None = None
    chunk_set_id: UUID | None = None
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    materialization_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    safe_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    symbol_name: str | None = Field(default=None, max_length=256)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_range(self) -> "EvidencePointerV1":
        if self.end_line < self.start_line:
            raise ValueError("evidence range is reversed")
        return self


class MemoryState(StrEnum):
    NEEDS_REVIEW = "needs_review"
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class MemoryReviewRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["accept", "reject", "archive", "restore", "supersede"]
    expected_version: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1024)
    replacement_id: UUID | None = None

    @model_validator(mode="after")
    def replacement_required(self) -> "MemoryReviewRequestV1":
        if (self.action == "supersede") != (self.replacement_id is not None):
            raise ValueError("only supersede requires a replacement")
        return self


class MemoryEntryV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_id: UUID
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    memory_kind: MemoryKind
    topic: str
    summary: str
    evidence_pointers: tuple[EvidencePointerV1, ...] = Field(default=(), max_length=10)
    created_at: datetime
    updated_at: datetime
    state: MemoryState = MemoryState.NEEDS_REVIEW
    version: int = Field(default=1, ge=1)
    expires_at: datetime | None = None
    accepted_at: datetime | None = None
    reviewer_id: UUID | None = None
    content_digest: str | None = None
    source_available: bool = True
    historical_hint: bool = False
    source_correspondence: Literal[
        "original_snapshot", "matching_files", "changed", "human_note"
    ] = "original_snapshot"


class MemoryCreateRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID | None = None
    repository_id: UUID | None = None
    snapshot_id: UUID | None = None
    principal_id: UUID | None = None
    memory_kind: MemoryKind
    topic: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=4096)
    request_key: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_pointers: tuple[EvidencePointerV1, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def bounded_summary_bytes(self) -> "MemoryCreateRequestV1":
        if len(self.summary.encode()) > 16384:
            raise ValueError("memory summary exceeds byte limit")
        return self


class MemoryQueryRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID | None = None
    repository_id: UUID | None = None
    principal_id: UUID | None = None
    snapshot_id: UUID | None = None
    query: str = Field(min_length=1, max_length=256)
    memory_kind: MemoryKind | None = None
    topic: str | None = None
    limit: int = Field(default=5, ge=1, le=10)
    history_mode: Literal["off", "current_only", "historical_hints"] = "current_only"


class MemoryQueryResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID
    repository_id: UUID
    query: str
    total_candidates: int = Field(ge=0)
    items: tuple[MemoryEntryV1, ...]
    scanned_count: int = Field(default=0, ge=0, le=100)
    omitted_count: int = Field(default=0, ge=0)
    partial: bool = False
    omission_reasons: tuple[str, ...] = ()


class MemoryManagementPageV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[MemoryEntryV1, ...]
    next_cursor: UUID | None = None
    can_review: bool = False


class MemoryReviewEventV1(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: int
    actor_id: UUID
    action: str
    reason: str
    created_at: datetime


class MemoryHistoryV1(BaseModel):
    model_config = ConfigDict(frozen=True)
    versions: tuple[MemoryEntryV1, ...]
    events: tuple[MemoryReviewEventV1, ...]
    partial: bool

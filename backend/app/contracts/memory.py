"""Typed contracts for evidence-backed repository memory."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryKind(StrEnum):
    TRIAGE_RESOLUTION = "triage_resolution"
    SECURITY_FINDING_EVIDENCE = "security_finding_evidence"
    ARCHITECTURE_FACT = "architecture_fact"


class EvidencePointerV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    finding_id: UUID | None = None
    chunk_id: UUID | None = None
    chunk_set_id: UUID | None = None
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    materialization_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    safe_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    symbol_name: str | None = None
    sha256: str | None = None


class MemoryEntryV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_id: UUID
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    memory_kind: MemoryKind
    topic: str
    summary: str
    evidence_pointers: tuple[EvidencePointerV1, ...] = ()
    created_at: datetime
    updated_at: datetime


class MemoryCreateRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID | None = None
    repository_id: UUID | None = None
    snapshot_id: UUID | None = None
    principal_id: UUID | None = None
    memory_kind: MemoryKind
    topic: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=4096)
    evidence_pointers: tuple[EvidencePointerV1, ...] = ()


class MemoryQueryRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID | None = None
    repository_id: UUID | None = None
    principal_id: UUID | None = None
    snapshot_id: UUID | None = None
    query: str = Field(min_length=1, max_length=256)
    memory_kind: MemoryKind | None = None
    topic: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class MemoryQueryResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID
    repository_id: UUID
    query: str
    total_candidates: int = Field(ge=0)
    items: tuple[MemoryEntryV1, ...]

"""Typed contracts for syntax-aware code chunking, token budgeting, and manifest creation."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChunkKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    WINDOW = "window"
    FILE = "file"


class ChunkProfileV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunker_version: str = "v1_syntax_aware"
    target_tokens: int = Field(default=768, ge=64, le=4096)
    max_tokens: int = Field(default=1024, ge=64, le=8192)
    max_bytes: int = Field(default=4096, ge=256, le=65536)
    overlap_tokens: int = Field(default=64, ge=0, le=256)
    tokenizer_name: str = "cl100k_base"

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.target_tokens > self.max_tokens:
            raise ValueError(
                f"target_tokens {self.target_tokens} cannot exceed max_tokens {self.max_tokens}"
            )
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError(
                f"overlap_tokens {self.overlap_tokens} must be strictly less than "
                f"target_tokens {self.target_tokens}"
            )
        return self


class SourceChunkV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    file_id: UUID
    file_path: str
    chunk_index: int = Field(ge=0)
    chunk_kind: ChunkKind
    symbol_name: str | None = None
    qualified_name: str | None = None
    cpg_node_id: UUID | None = None
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_sha256: str
    byte_count: int = Field(ge=0)
    token_count: int = Field(ge=0)
    raw_content: bytes
    safe_content: str
    redaction_digest: str
    chunker_version: str

    @model_validator(mode="after")
    def validate_byte_bounds(self) -> Self:
        if self.start_byte > self.end_byte:
            raise ValueError(f"start_byte {self.start_byte} cannot exceed end_byte {self.end_byte}")
        if self.start_line > self.end_line:
            raise ValueError(f"start_line {self.start_line} cannot exceed end_line {self.end_line}")
        return self


class ChunkManifestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    chunk_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    profile: ChunkProfileV1
    manifest_sha256: str

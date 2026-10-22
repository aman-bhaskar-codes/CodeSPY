"""Typed contracts for exact symbol/path search and PostgreSQL lexical search."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.chunking import ChunkKind


class SearchMode(StrEnum):
    EXACT = "exact"
    LEXICAL = "lexical"
    HYBRID = "hybrid"


class ExactSearchType(StrEnum):
    FILE_PATH = "file_path"
    PATH_SEGMENT = "path_segment"
    SYMBOL = "symbol"
    QUALIFIED_NAME = "qualified_name"


class SearchScopeV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID


class ExactSearchRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1
    query: str = Field(min_length=1, max_length=256)
    search_type: ExactSearchType | None = None
    limit: int = Field(default=20, ge=1, le=100)


class LexicalSearchRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=40, ge=1, le=100)


class SearchHitV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    file_id: UUID
    file_path: str
    chunk_kind: ChunkKind
    symbol_name: str | None = None
    qualified_name: str | None = None
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    score: float = Field(ge=0.0)
    snippet: str
    content_sha256: str
    match_reasons: tuple[str, ...] = ()


class SearchResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1
    mode: SearchMode
    query: str
    total_candidates: int = Field(ge=0)
    hits: tuple[SearchHitV1, ...]
    coverage_status: str = "complete"
    omitted_count: int = Field(default=0, ge=0)
    execution_ms: float = Field(default=0.0, ge=0.0)

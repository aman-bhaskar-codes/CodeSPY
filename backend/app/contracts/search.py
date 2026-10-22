from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.chunking import ChunkKind


class SearchMode(StrEnum):
    EXACT = "exact"
    LEXICAL = "lexical"
    DENSE = "dense"
    HYBRID = "hybrid"


class IndexState(StrEnum):
    BUILDING = "building"
    SEALING = "sealing"
    READY = "ready"
    PARTIAL_READY = "partial_ready"
    FAILED = "failed"
    STALE = "stale"


class IndexType(StrEnum):
    LEXICAL = "lexical"
    DENSE = "dense"
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

    scope: SearchScopeV1 | None = None
    query: str = Field(min_length=1, max_length=256)
    search_type: ExactSearchType | None = None
    limit: int = Field(default=20, ge=1, le=100)


class LexicalSearchRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1 | None = None
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


class SearchIndexRecordV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    index_id: UUID
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    index_type: IndexType
    state: IndexState
    embedding_provider: str | None = None
    embedding_model: str | None = None
    dimension: int | None = None
    total_chunks: int = 0
    indexed_chunks: int = 0
    failed_chunks: int = 0
    sealed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class DenseSearchRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1 | None = None
    query: str = Field(min_length=1, max_length=1024)
    index_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class DenseSearchHitV1(BaseModel):
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
    score: float = Field(ge=0.0, le=1.0)
    snippet: str
    content_sha256: str


class DenseSearchResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1
    query: str
    index_id: UUID
    embedding_model: str
    total_candidates: int = Field(ge=0)
    hits: tuple[DenseSearchHitV1, ...]
    execution_ms: float = Field(default=0.0, ge=0.0)


class HybridSearchRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1 | None = None
    query: str = Field(min_length=1, max_length=1024)
    limit: int = Field(default=20, ge=1, le=100)
    rrf_k: int = Field(default=60, ge=1, le=200)
    weights: dict[str, float] = Field(
        default_factory=lambda: {"exact": 1.5, "lexical": 1.0, "dense": 1.0}
    )
    exact_limit: int = Field(default=20, ge=1, le=100)
    lexical_limit: int = Field(default=30, ge=1, le=100)
    dense_limit: int = Field(default=30, ge=1, le=100)
    dense_index_id: UUID | None = None


class HybridSearchHitV1(BaseModel):
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
    match_modalities: tuple[str, ...]
    modality_ranks: dict[str, int]


class HybridSearchResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1
    query: str
    total_candidates: int = Field(ge=0)
    hits: tuple[HybridSearchHitV1, ...]
    execution_ms: float = Field(default=0.0, ge=0.0)

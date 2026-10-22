"""Typed contracts for context assembly, token budgeting, and grounding packets."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.chunking import ChunkKind
from app.contracts.search import HybridSearchHitV1, SearchScopeV1


class ContextItemV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    file_id: UUID
    file_path: str
    symbol_name: str | None = None
    qualified_name: str | None = None
    chunk_kind: ChunkKind
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    token_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    safe_content: str
    citation_key: str
    relevance_score: float = Field(ge=0.0)
    match_modalities: tuple[str, ...]


class ContextBudgetV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_tokens: int = Field(default=4096, ge=128, le=32768)
    max_bytes: int = Field(default=32768, ge=512, le=262144)
    reserved_overhead_tokens: int = Field(default=256, ge=0)
    tokenizer_name: str = "cl100k_base"


class ContextAssemblyRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: SearchScopeV1 | None = None
    query: str
    candidate_hits: tuple[HybridSearchHitV1, ...] = ()
    budget: ContextBudgetV1 = Field(default_factory=ContextBudgetV1)
    deduplicate_files: bool = True
    system_preamble: str | None = None


class ContextPacketV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    packet_id: UUID
    scope: SearchScopeV1
    query: str
    system_preamble: str | None = None
    items: tuple[ContextItemV1, ...]
    total_tokens: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    items_count: int = Field(ge=0)
    omitted_count: int = Field(ge=0)
    packet_sha256: str
    created_at: datetime

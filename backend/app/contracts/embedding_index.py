"""Contracts for the fenced dense index job: an immutable spec bound to one sealed chunk set."""

import hashlib
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field

from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract

DIMENSIONS = (256, 512, 1024)


class EmbedConfigV1(Contract):
    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    dimension: int
    normalize: bool = True
    price_version: str = Field(min_length=1, max_length=128)

    def validated(self) -> "EmbedConfigV1":
        if self.dimension not in DIMENSIONS:
            raise ValueError("unsupported embedding dimension")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump(mode="json"))


class EmbedSpecV1(Contract):
    """What one dense publication embeds, for whom, and with which profile."""

    schema_version: Literal[1] = 1
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID
    chunk_set_id: UUID
    config: EmbedConfigV1

    def lexical_index_id(self) -> UUID:
        return uuid5(self.chunk_set_id, "lexical-v1")

    def dense_index_id(self) -> UUID:
        c = self.config
        return uuid5(self.chunk_set_id, f"dense-v1:{c.provider}:{c.model_id}:{c.dimension}")

    def spec_json(self) -> dict[str, object]:
        return {
            "org_id": str(self.org_id),
            "repository_id": str(self.repository_id),
            "snapshot_id": str(self.snapshot_id),
            "principal_id": str(self.principal_id),
            "chunk_set_id": str(self.chunk_set_id),
            "config": self.config.model_dump(mode="json"),
        }

    def canonical_text(self) -> str:
        return canonical_bytes(self.spec_json()).decode()

    def spec_hash(self) -> str:
        return hashlib.sha256(self.canonical_text().encode()).hexdigest()


class EmbedReceiptV1(Contract):
    schema_version: Literal[1] = 1
    org_id: UUID
    job_id: UUID
    spec_id: UUID
    chunk_set_id: UUID
    index_id: UUID
    chunk_count: int
    token_total: int
    coverage: Literal["complete", "partial"]
    embedding_digest: str
    ledger_digest: str

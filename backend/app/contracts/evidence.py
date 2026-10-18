from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract, RunRef
from app.domain.source_ranges import SourceRangeV1


class EvidenceEnvelopeV1(Contract):
    schema_version: Literal[1] = 1
    scope: RunRef
    id: UUID
    file_id: UUID | None = None
    range: SourceRangeV1 | None = None
    provider_name: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)
    observation_mode: Literal["static", "runtime", "external"]
    basis: Literal["observed", "derived", "inferred"]
    coverage_status: Literal["complete", "partial", "unknown", "unavailable"]
    payload: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.range and not self.file_id:
            raise ValueError("range requires file identity")
        for text in (self.provider_name, self.provider_version, self.operation):
            if len(text.encode()) > 128:
                raise ValueError("provider field too long")
        if len(canonical_bytes(self.payload)) > 65536 or any(
            len(x.encode()) > 2048 for x in self.limitations
        ):
            raise ValueError("evidence exceeds bounds")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump(exclude={"id", "digest", "created_at"}))


class EvidenceRef(Contract):
    scope: RunRef
    evidence_id: UUID


class EvidenceRecord(EvidenceEnvelopeV1):
    digest: str
    created_at: datetime

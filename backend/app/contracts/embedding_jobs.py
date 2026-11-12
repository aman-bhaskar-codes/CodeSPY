"""Versioned contracts for the dense embed job lifecycle (enqueue, queue envelope, record)."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.contracts.indexing import JobState
from app.contracts.scanners import Digest
from app.domain.canonical_json import canonical_bytes
from app.domain.scope import Contract


class EnqueueEmbedJobV7(Contract):
    repository_id: UUID
    snapshot_id: UUID
    embed_spec_id: UUID
    config_hash: Digest
    operation: Literal["embed_chunk_set"] = "embed_chunk_set"
    request_key: str = Field(min_length=1, max_length=128)
    retry_of_job_id: UUID | None = None

    @model_validator(mode="after")
    def request_identity(self) -> Self:
        if len(self.request_key.encode()) > 128 or self.request_key.startswith(("job:", "child:")):
            raise ValueError("invalid embed request key")
        return self


class JobEnvelopeV7(Contract):
    """Queue envelope: identity, spec and deadline only; no source, vector or model content."""

    schema_version: Literal[7] = 7
    org_id: UUID
    job_id: UUID
    embed_spec_id: UUID
    operation: Literal["embed_chunk_set"] = "embed_chunk_set"
    config_hash: Digest
    dispatch_generation: int = Field(gt=0)
    deadline: AwareDatetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > 4096:
            raise ValueError("queue envelope exceeds bound")
        return self


class EmbedJobRecordV7(Contract):
    schema_version: Literal[7] = 7
    org_id: UUID
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    embed_spec_id: UUID
    operation: Literal["embed_chunk_set"] = "embed_chunk_set"
    state: JobState
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    deadline: AwareDatetime
    failure_code: str | None
    result_index_id: UUID | None
    created_at: AwareDatetime
    finished_at: AwareDatetime | None

    @model_validator(mode="after")
    def terminal_result(self) -> Self:
        terminal = self.state in {"succeeded", "partial", "failed", "cancelled"}
        success = self.state in {"succeeded", "partial"}
        if (
            terminal != (self.finished_at is not None)
            or success != (self.result_index_id is not None)
            or self.attempt_count > self.max_attempts
        ):
            raise ValueError("inconsistent embed job state")
        return self

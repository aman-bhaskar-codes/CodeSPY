from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.canonical_json import canonical_bytes
from app.domain.scope import Contract, RunRef


class JobEnvelopeV2(Contract):
    schema_version: Literal[2] = 2
    org_id: UUID
    job_id: UUID
    operation: Literal["admit_source", "inventory_snapshot"]
    dispatch_generation: int = Field(gt=0)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deadline: AwareDatetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > 4096:
            raise ValueError("queue envelope exceeds bound")
        return self


class AttemptLeaseV2(Contract):
    schema_version: Literal[2] = 2
    org_id: UUID
    job_id: UUID
    attempt_id: UUID
    fence: int = Field(gt=0)
    owner: str = Field(min_length=1, max_length=128)
    lease_expires_at: AwareDatetime
    heartbeat_at: AwareDatetime
    run_ref: RunRef | None = None


class EnqueueJobV2(Contract):
    repository_id: UUID
    operation: Literal["admit_source", "inventory_snapshot"]
    request_key: str = Field(min_length=1, max_length=128)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_id: UUID | None = None
    snapshot_id: UUID | None = None
    parent_job_id: UUID | None = None
    retry_of_job_id: UUID | None = None

    @model_validator(mode="after")
    def operation_source(self) -> Self:
        if self.operation == "admit_source" and self.submission_id is None:
            raise ValueError("admission requires a submission")
        if self.operation == "inventory_snapshot" and self.snapshot_id is None:
            raise ValueError("inventory requires a snapshot")
        if len(self.request_key.encode()) > 128:
            raise ValueError("request key exceeds bound")
        return self


class JobRecordV2(Contract):
    org_id: UUID
    id: UUID
    repository_id: UUID
    submission_id: UUID | None
    snapshot_id: UUID | None
    operation: Literal["admit_source", "inventory_snapshot"]
    state: Literal[
        "queued",
        "running",
        "retry_wait",
        "cancelling",
        "succeeded",
        "partial",
        "failed",
        "cancelled",
    ]
    attempt_count: int
    max_attempts: int
    deadline: AwareDatetime
    failure_code: str | None
    result_run_id: UUID | None
    inventory_job_id: UUID | None = None
    created_at: AwareDatetime
    finished_at: AwareDatetime | None


class RetryResponseV1(Contract):
    schema_version: Literal[1] = 1
    retry_job_id: UUID
    submission_id: UUID
    retry_of_job_id: UUID
    retry_of_submission_id: UUID | None = None

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

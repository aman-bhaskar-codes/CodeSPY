"""Versioned boundary proposals only; no corresponding tables or workers."""

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.contracts.evidence import EvidenceRef
from app.domain.scope import Contract, RunRef
from app.domain.source_ranges import SourceRangeV1


class FindingDraftV1(Contract):
    schema_version: Literal[1] = 1
    scope: RunRef
    rule: str
    family: str
    title: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    severity_basis: str
    locations: list[SourceRangeV1]
    support: list[EvidenceRef]
    verification_disposition: Literal[
        "proposed", "supported-static", "verified-scenario", "contradicted", "unresolved"
    ] = "proposed"
    remediation: str


class JobEnvelopeV1(Contract):
    schema_version: Literal[1] = 1
    org_id: UUID
    job_id: UUID
    snapshot_id: UUID
    run_id: UUID
    operation: str
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deadline: AwareDatetime


class AttemptLeaseV1(Contract):
    job_id: UUID
    attempt_id: UUID
    fence: int = Field(gt=0)
    owner: str
    expiry: AwareDatetime
    heartbeat: AwareDatetime


class ProgressEventV1(Contract):
    schema_version: Literal[1] = 1
    run_id: UUID
    sequence: int = Field(ge=1)
    event_type: str
    timestamp: AwareDatetime
    public_payload: dict[str, Any]


class EmbeddingProfileV1(Contract):
    provider: str
    model_id: str
    revision: str
    dimensions: int = Field(gt=0)
    distance: Literal["cosine", "l2", "inner_product"]
    normalization: str
    element_type: Literal["float32", "float16"]
    chunker_version: str
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ChunkRefV1(Contract):
    scope: RunRef
    file_id: UUID
    range: SourceRangeV1
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

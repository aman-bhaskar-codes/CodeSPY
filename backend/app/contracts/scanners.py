"""Metadata-only scanner transport. Valid DTOs are not source attestation.

The broker must additionally authenticate file bytes/ranges and the qualified
profile. No raw scanner messages, matches, secrets or executable input belong here.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract, RunRef
from app.domain.source_ranges import SourceRangeV1

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RuleId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")]


class ScannerProfileV1(Contract):
    tool: Literal["semgrep", "gitleaks"]
    artifact_sha256: Digest
    rules_sha256: Digest
    rule_ids: tuple[RuleId, ...] = Field(min_length=1, max_length=128)
    memory_bytes: int = Field(default=1073741824, ge=1, le=1073741824)
    cpu: int = Field(default=2, ge=1, le=2)
    pids: int = Field(default=64, ge=1, le=64)
    deadline_seconds: int = Field(default=180, ge=1, le=180)
    output_bytes: int = Field(default=2097152, ge=1, le=2097152)

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        if len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("duplicate rule identity")
        return self


class ScannerFileV1(Contract):
    file_id: UUID
    sha256: Digest
    size_bytes: int = Field(ge=0, le=2097152)


class ScannerRequestV1(Contract):
    schema_version: Literal[1] = 1
    scope: RunRef
    repository_id: UUID
    config_hash: Digest
    profile: ScannerProfileV1
    files: tuple[ScannerFileV1, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len({f.file_id for f in self.files}) != len(self.files):
            raise ValueError("duplicate file identity")
        if sum(f.size_bytes for f in self.files) > 16777216:
            raise ValueError("source byte budget exceeded")
        if len(canonical_bytes(self.model_dump())) > 65536:
            raise ValueError("request byte budget exceeded")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())


class ScannerObservationV1(Contract):
    rule_id: RuleId
    file_id: UUID
    sha256: Digest
    range: SourceRangeV1


class ScannerResultV1(Contract):
    schema_version: Literal[1] = 1
    request_digest: Digest
    status: Literal["completed", "partial", "failed", "unavailable", "cancelled"]
    processed_file_ids: tuple[UUID, ...] = Field(max_length=128)
    omitted_file_ids: tuple[UUID, ...] = Field(max_length=128)
    observations: tuple[ScannerObservationV1, ...] = Field(default=(), max_length=1000)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        processed, omitted = set(self.processed_file_ids), set(self.omitted_file_ids)
        if len(processed) != len(self.processed_file_ids) or len(omitted) != len(
            self.omitted_file_ids
        ):
            raise ValueError("duplicate coverage identity")
        if processed & omitted:
            raise ValueError("overlapping coverage")
        if self.status == "completed" and omitted:
            raise ValueError("completed scan cannot omit selected files")
        if self.status not in {"completed", "partial"} and self.observations:
            raise ValueError("unsuccessful scan cannot publish observations")
        if len(canonical_bytes(self.model_dump())) > 2097152:
            raise ValueError("result byte budget exceeded")
        return self

    def validate_for(self, request: ScannerRequestV1) -> None:
        """Reject untrusted cross-request output before source attestation.

        Bounds and metadata checks cannot establish that a tool actually scanned
        a file or that claimed line/columns agree with its UTF-8 bytes.
        """
        if self.request_digest != request.semantic_hash():
            raise ValueError("scanner request identity differs")
        files = {f.file_id: f for f in request.files}
        processed, omitted = set(self.processed_file_ids), set(self.omitted_file_ids)
        if processed | omitted != files.keys():
            raise ValueError("coverage differs from selected source")
        if len(canonical_bytes(self.model_dump())) > request.profile.output_bytes:
            raise ValueError("profile output budget exceeded")
        allowed_rules = set(request.profile.rule_ids)
        seen: set[bytes] = set()
        for observation in self.observations:
            source = files.get(observation.file_id)
            if source is None or observation.file_id not in processed:
                raise ValueError("observation requires processed selected source")
            if observation.rule_id not in allowed_rules:
                raise ValueError("observation rule is not qualified")
            if observation.sha256 != source.sha256:
                raise ValueError("observation source hash differs")
            if observation.range.end_byte > source.size_bytes:
                raise ValueError("observation range exceeds source")
            identity = canonical_bytes(observation.model_dump())
            if identity in seen:
                raise ValueError("duplicate observation")
            seen.add(identity)

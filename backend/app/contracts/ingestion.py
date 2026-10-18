"""Byte inventory and admission contracts, independent of SQLAlchemy."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.canonical_json import semantic_digest
from app.domain.scope import Contract


class AdmissionPolicyV1(Contract):
    version: Literal["source-zip-v1"] = "source-zip-v1"
    max_archive_bytes: int = Field(default=104857600, gt=0, le=104857600)
    max_expanded_bytes: int = Field(default=1073741824, gt=0, le=1073741824)
    max_entries: int = Field(default=50000, gt=0, le=50000)
    max_directory_bytes: int = 16777216
    max_file_bytes: int = 104857600
    eligible_file_bytes: int = 2097152
    max_ratio: int = 100
    max_depth: int = 32
    max_path_bytes: int = 1024
    max_manifest_bytes: int = 67108864
    github_strip_root: bool = False

    def digest(self) -> str:
        return semantic_digest(self.model_dump())


class UploadReservationV1(Contract):
    schema_version: Literal[1] = 1
    repository_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    declared_size: int = Field(ge=1, le=104857600)
    declared_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(self.filename.encode()) > 255:
            raise ValueError("filename exceeds bound")
        return self


Reason = Literal[
    "oversized",
    "unsupported_language",
    "binary",
    "nested_archive",
    "sensitive_path",
    "generated_dependency",
]


class ManifestEntryV1(Contract):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    language_hint: str | None = None
    eligible: bool
    reason: Reason | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.eligible != (self.reason is None):
            raise ValueError("eligibility and reason disagree")
        return self


class ArtifactManifestV1(Contract):
    schema_version: Literal[1] = 1
    policy_version: str
    policy_hash: str
    archive_sha256: str
    archive_size: int
    normalized_files_sha256: str
    source_revision: str | None = None
    github_repository_id: int | None = None
    entries: list[ManifestEntryV1]
    total_entries: int


class ObjectDescriptor(Contract):
    key: str
    sha256: str
    size_bytes: int
    version_id: str | None = None

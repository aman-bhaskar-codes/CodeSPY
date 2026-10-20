"""Byte inventory and admission contracts, independent of SQLAlchemy."""

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract
from app.security.archive_paths import collision_key, validate_archive_path


class _AdmissionLimits(Contract):
    max_archive_bytes: int = Field(default=104857600, gt=0, le=104857600)
    max_expanded_bytes: int = Field(default=1073741824, gt=0, le=1073741824)
    max_entries: int = Field(default=10000, gt=0, le=10000)
    max_directory_bytes: int = Field(default=16777216, gt=0, le=16777216)
    max_file_bytes: int = Field(default=104857600, gt=0, le=104857600)
    eligible_file_bytes: int = Field(default=2097152, gt=0, le=2097152)
    max_ratio: int = Field(default=100, gt=0, le=100)
    max_depth: int = Field(default=32, gt=0, le=32)
    max_path_bytes: int = Field(default=1024, gt=0, le=1024)
    max_manifest_bytes: int = Field(default=67108864, gt=0, le=67108864)
    github_strip_root: bool = False

    def digest(self) -> str:
        return semantic_digest(self.model_dump())


class AdmissionPolicyV1(_AdmissionLimits):
    version: Literal["source-zip-v1"] = "source-zip-v1"


class AdmissionPolicyV2(_AdmissionLimits):
    """Future admissions may retain exact uv.lock as bounded TOML metadata."""

    version: Literal["source-zip-v2"] = "source-zip-v2"
    metadata_file_bytes: int = Field(default=262144, gt=0, le=262144)


AdmissionPolicy = Annotated[AdmissionPolicyV1 | AdmissionPolicyV2, Field(discriminator="version")]
_POLICY_ADAPTER: TypeAdapter[AdmissionPolicy] = TypeAdapter(AdmissionPolicy)


def parse_admission_policy(value: Any) -> AdmissionPolicy:
    # V1 historically allowed its version field to be omitted. Never infer V2
    # from fields: absent version retains the strict original V1 contract.
    if isinstance(value, dict) and "version" not in value:
        return AdmissionPolicyV1.model_validate(value)
    return _POLICY_ADAPTER.validate_python(value)


def resolve_admission_policy(digest: str, *, github_strip_root: bool) -> AdmissionPolicy:
    """Resolve only registered immutable default policies from durable hashes.

    Never substitute today's policy for an old snapshot or guess at unknown
    configuration. Custom reduced limits need a separately persisted registry.
    """
    for policy in (
        AdmissionPolicyV1(github_strip_root=github_strip_root),
        AdmissionPolicyV2(github_strip_root=github_strip_root),
    ):
        if policy.digest() == digest:
            return policy
    raise ValueError("unsupported admission policy")


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
    path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0, le=104857600)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    language_hint: str | None = Field(default=None, max_length=128)
    eligible: bool
    reason: Reason | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        validate_archive_path(self.path)
        if self.path.endswith("/"):
            raise ValueError("manifest entries must be regular files")
        if self.eligible != (self.reason is None):
            raise ValueError("eligibility and reason disagree")
        return self


class ArtifactManifestV1(Contract):
    schema_version: Literal[1] = 1
    policy_version: Literal["source-zip-v1", "source-zip-v2"]
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size: int = Field(ge=22, le=104857600)
    normalized_files_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    github_repository_id: int | None = Field(default=None, gt=0)
    entries: list[ManifestEntryV1] = Field(max_length=10000)
    total_entries: int = Field(ge=0, le=10000)

    @model_validator(mode="after")
    def complete_manifest(self) -> Self:
        paths = [e.path for e in self.entries]
        if paths != sorted(paths, key=lambda p: p.encode()) or len(
            {collision_key(p) for p in paths}
        ) != len(paths):
            raise ValueError("manifest path ordering or collision differs")
        if self.total_entries < len(self.entries):
            raise ValueError("manifest entry counts differ")
        normalized = [
            {
                "path": e.path,
                "sha256": e.sha256,
                "size_bytes": e.size_bytes,
                "language": e.language_hint,
            }
            for e in self.entries
        ]
        if semantic_digest(normalized) != self.normalized_files_sha256:
            raise ValueError("normalized file digest differs")
        if len(canonical_bytes(self.model_dump())) > 67108864:
            raise ValueError("manifest exceeds byte bound")
        return self


class ObjectDescriptor(Contract):
    key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=104857600)
    version_id: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def private_key(self) -> Self:
        if (
            len(self.key.encode()) > 1024
            or any(c in self.key for c in ("\\", "?", "#", ":", "\x00"))
            or any(p in {"", ".", ".."} for p in self.key.split("/"))
        ):
            raise ValueError("generated opaque object key required")
        return self


class SubmissionRecordV1(Contract):
    schema_version: Literal[1] = 1
    org_id: UUID
    id: UUID
    repository_id: UUID
    source_kind: Literal["upload_zip", "github_archive"]
    state: Literal[
        "reserved",
        "uploading",
        "uploaded",
        "admission_queued",
        "admitting",
        "admitted",
        "rejected",
        "failed",
        "expired",
        "cancelled",
    ]
    declared_size: int | None
    actual_size: int | None
    actual_sha: str | None
    snapshot_id: UUID | None
    error_code: str | None
    job_id: UUID | None = None

"""Bounded offline dependency evidence; complete covers selected lockfiles only."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.contracts.scanners import Digest
from app.contracts.structure import SourceDescriptorV1, SourceFileV1, SourcePolicyV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract, RunRef


class PackageQuery(Contract):
    ecosystem: Literal["npm", "PyPI"]
    name: str = Field(min_length=1, max_length=214)
    version: str = Field(min_length=1, max_length=128)

    def key(self) -> tuple[str, str, str]:
        return self.ecosystem, self.name, self.version


class AdvisoryAssessment(Contract):
    status: Literal["matched", "empty", "missing", "stale", "incomplete", "withdrawn", "invalid"]
    advisories: tuple[tuple[str, Digest], ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == "matched") != bool(self.advisories):
            raise ValueError("advisory support differs from status")
        identities = [identity for identity, _ in self.advisories]
        if len(set(identities)) != len(identities) or any(
            not 1 <= len(identity) <= 128 for identity in identities
        ):
            raise ValueError("invalid advisory identity")
        return self


class DependencyFileV1(Contract):
    file_id: UUID
    sha256: Digest
    size_bytes: int = Field(ge=0, le=262144)
    format: Literal["package-lock-v3", "uv-v1-r3"]


class DependencyRequestV1(Contract):
    schema_version: Literal[1] = 1
    scope: RunRef
    repository_id: UUID
    config_hash: Digest
    runtime_digest: Digest
    bundle_digest: Digest
    evaluated_at: AwareDatetime
    parser_profile: Literal["lockfile-offline-v1"] = "lockfile-offline-v1"
    files: tuple[DependencyFileV1, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len({f.file_id for f in self.files}) != len(self.files):
            raise ValueError("duplicate metadata identity")
        if sum(f.size_bytes for f in self.files) > 2097152:
            raise ValueError("metadata byte budget exceeded")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())


class DependencyArchiveV1(Contract):
    request: DependencyRequestV1
    archive: SourceDescriptorV1
    manifest: SourceDescriptorV1
    files: tuple[SourceFileV1, ...] = Field(max_length=10000)
    policy: SourcePolicyV1

    @model_validator(mode="after")
    def selection(self) -> Self:
        files = {f.file_id: f for f in self.files}
        if len(files) != len(self.files) or len({f.path for f in self.files}) != len(self.files):
            raise ValueError("duplicate stored metadata identity")
        for selected in self.request.files:
            stored = files.get(selected.file_id)
            if stored is None or (stored.sha256, stored.size_bytes) != (
                selected.sha256,
                selected.size_bytes,
            ):
                raise ValueError("metadata selection differs from persisted source")
            expected = "uv.lock" if selected.format == "uv-v1-r3" else "package-lock.json"
            if stored.path.rsplit("/", 1)[-1] != expected:
                raise ValueError("metadata parser differs from persisted path")
        if len(canonical_bytes(self.model_dump())) > 8388608:
            raise ValueError("metadata envelope exceeds budget")
        return self


class DependencyFileOutcomeV1(Contract):
    file_id: UUID
    status: Literal["parsed", "excluded", "unsupported_or_invalid"]


class DependencyPackageV1(Contract):
    file_id: UUID
    location: str = Field(min_length=1, max_length=1024)
    query: PackageQuery
    marker_count: int = Field(default=0, ge=0, le=32)
    environment_status: Literal["unresolved"] = "unresolved"
    assessment: AdvisoryAssessment


class UnresolvedDependencyV1(Contract):
    file_id: UUID
    location: str = Field(min_length=1, max_length=1024)
    reason: Literal["unqualified_identity"] = "unqualified_identity"


class DependencyResultV1(Contract):
    schema_version: Literal[1] = 1
    request_digest: Digest
    coverage: Literal["complete", "partial", "unknown"]
    files: tuple[DependencyFileOutcomeV1, ...] = Field(max_length=32)
    packages: tuple[DependencyPackageV1, ...] = Field(max_length=5000)
    unresolved: tuple[UnresolvedDependencyV1, ...] = Field(default=(), max_length=5000)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > 2097152:
            raise ValueError("dependency result exceeds budget")
        if len(self.packages) + len(self.unresolved) > 5000:
            raise ValueError("dependency instance budget exceeded")
        return self

    def validate_for(self, request: DependencyRequestV1) -> None:
        files = {f.file_id: f for f in request.files}
        outcomes = {f.file_id: f.status for f in self.files}
        if (
            self.request_digest != request.semantic_hash()
            or len(outcomes) != len(self.files)
            or outcomes.keys() != files.keys()
        ):
            raise ValueError("dependency result identity differs")
        instances: tuple[DependencyPackageV1 | UnresolvedDependencyV1, ...] = (
            *self.packages,
            *self.unresolved,
        )
        identities = [(p.file_id, p.location) for p in instances]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate dependency instance")
        if any(outcomes.get(p.file_id) != "parsed" for p in instances):
            raise ValueError("dependency requires parsed selected input")
        for package in self.packages:
            expected = "PyPI" if files[package.file_id].format == "uv-v1-r3" else "npm"
            if package.query.ecosystem != expected:
                raise ValueError("dependency ecosystem differs from parser")
        complete = (
            bool(files)
            and all(status == "parsed" for status in outcomes.values())
            and not self.unresolved
            and all(p.assessment.status in {"matched", "empty"} for p in self.packages)
        )
        expected_coverage = (
            "complete" if complete else "partial" if "parsed" in outcomes.values() else "unknown"
        )
        if self.coverage != expected_coverage:
            raise ValueError("dependency coverage differs from support")

"""Reusable scan selections and scan-only lifecycle transport."""

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.contracts.dependencies import DependencyFileV1, DependencyRequestV1
from app.contracts.findings import ScanPublicationSpecV1
from app.contracts.scanners import Digest, ScannerFileV1, ScannerProfileV1, ScannerRequestV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract, RunRef


class NativeSelectionV1(Contract):
    profile: ScannerProfileV1
    files: tuple[ScannerFileV1, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if (
            len({f.file_id for f in self.files}) != len(self.files)
            or sum(f.size_bytes for f in self.files) > 16777216
        ):
            raise ValueError("invalid native selection")
        object.__setattr__(self, "files", tuple(sorted(self.files, key=lambda f: str(f.file_id))))
        return self


class DependencySelectionV1(Contract):
    parser_profile: Literal["lockfile-offline-v1"] = "lockfile-offline-v1"
    bundle_digest: Digest
    files: tuple[DependencyFileV1, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if (
            len({f.file_id for f in self.files}) != len(self.files)
            or sum(f.size_bytes for f in self.files) > 2097152
        ):
            raise ValueError("invalid dependency selection")
        object.__setattr__(self, "files", tuple(sorted(self.files, key=lambda f: str(f.file_id))))
        return self


class ScanConfigV1(Contract):
    schema_version: Literal[1] = 1
    runtime_digest: Digest
    catalog_digest: Digest
    native_selections: tuple[NativeSelectionV1, ...] = Field(default=(), max_length=2)
    dependency_selection: DependencySelectionV1 | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not self.native_selections and self.dependency_selection is None:
            raise ValueError("scan requires an operation")
        if len({n.profile.tool for n in self.native_selections}) != len(self.native_selections):
            raise ValueError("duplicate scan operation")
        object.__setattr__(
            self,
            "native_selections",
            tuple(sorted(self.native_selections, key=lambda n: n.profile.tool)),
        )
        metadata: dict[UUID, tuple[str, int]] = {}
        selections: tuple[NativeSelectionV1 | DependencySelectionV1, ...] = self.native_selections
        if self.dependency_selection is not None:
            selections += (self.dependency_selection,)
        for selection in selections:
            for file in selection.files:
                identity = file.sha256, file.size_bytes
                if file.file_id in metadata and metadata[file.file_id] != identity:
                    raise ValueError("conflicting scan source identity")
                metadata[file.file_id] = identity
        if len(canonical_bytes(self.model_dump())) > 65536:
            raise ValueError("scan configuration exceeds bound")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())

    def publication_spec(
        self, scope: RunRef, repository_id: UUID, captured_at: datetime
    ) -> ScanPublicationSpecV1:
        config_hash = self.semantic_hash()
        dependency = self.dependency_selection
        return ScanPublicationSpecV1(
            scope=scope,
            repository_id=repository_id,
            config_hash=config_hash,
            runtime_digest=self.runtime_digest,
            catalog_digest=self.catalog_digest,
            captured_at=captured_at,
            native_requests=tuple(
                ScannerRequestV1(
                    scope=scope,
                    repository_id=repository_id,
                    config_hash=config_hash,
                    profile=selection.profile,
                    files=selection.files,
                )
                for selection in self.native_selections
            ),
            dependency_request=DependencyRequestV1(
                scope=scope,
                repository_id=repository_id,
                config_hash=config_hash,
                runtime_digest=self.runtime_digest,
                bundle_digest=dependency.bundle_digest,
                evaluated_at=captured_at,
                parser_profile=dependency.parser_profile,
                files=dependency.files,
            )
            if dependency is not None
            else None,
        )


class ScanSpecRecordV1(Contract):
    org_id: UUID
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID
    config: ScanConfigV1
    config_hash: Digest
    created_at: AwareDatetime

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.config_hash != self.config.semantic_hash():
            raise ValueError("scan spec digest differs")
        return self


class EnqueueScanJobV4(Contract):
    repository_id: UUID
    snapshot_id: UUID
    scan_spec_id: UUID
    config_hash: Digest
    operation: Literal["scan_snapshot"] = "scan_snapshot"
    request_key: str = Field(min_length=1, max_length=128)
    retry_of_job_id: UUID | None = None

    @model_validator(mode="after")
    def key_bound(self) -> Self:
        if len(self.request_key.encode()) > 128:
            raise ValueError("request key exceeds bound")
        return self


class JobEnvelopeV4(Contract):
    schema_version: Literal[4] = 4
    org_id: UUID
    job_id: UUID
    operation: Literal["scan_snapshot"] = "scan_snapshot"
    scan_spec_id: UUID
    config_hash: Digest
    dispatch_generation: int = Field(gt=0)
    deadline: AwareDatetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > 4096:
            raise ValueError("queue envelope exceeds bound")
        return self


class ScanJobRecordV4(Contract):
    schema_version: Literal[4] = 4
    org_id: UUID
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    scan_spec_id: UUID
    operation: Literal["scan_snapshot"] = "scan_snapshot"
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
    created_at: AwareDatetime
    finished_at: AwareDatetime | None
    report_available: bool = False


class RetryScanResponseV3(Contract):
    schema_version: Literal[3] = 3
    retry_job_id: UUID
    retry_of_job_id: UUID
    scan_spec_id: UUID


def stored_config(value: Any) -> ScanConfigV1:
    return ScanConfigV1.model_validate_json(canonical_bytes(value))

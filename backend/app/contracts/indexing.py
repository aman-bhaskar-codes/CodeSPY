"""Bounded lexical-index contracts; validation is not worker or source attestation."""

import hashlib
from typing import Final, Literal, Self
from uuid import UUID, uuid5

from pydantic import AwareDatetime, ConfigDict, Field, computed_field, model_validator

from app.contracts.ingestion import ObjectDescriptor
from app.contracts.scanners import Digest
from app.contracts.source_view import PrivacyPolicyV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract
from app.security.archive_paths import collision_key, validate_archive_path

INDEX_NAMESPACE = UUID("ddf1b375-920d-427d-a0cc-706f8f95ad79")
TOKENIZER_SHA256: Final = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
OmissionReason = Literal[
    "source_limit",
    "sensitive_path",
    "unsupported_encoding",
    "chunk_limit",
    "safe_byte_limit",
    "manifest_limit",
    "draft_limit",
    "unmappable_range",
    "placeholder_limit",
]
JobState = Literal[
    "queued", "running", "retry_wait", "cancelling", "succeeded", "partial", "failed", "cancelled"
]


class IndexPrivacyPolicyV1(PrivacyPolicyV1):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class IndexBuildConfigV1(Contract):
    schema_version: Literal[1] = 1
    runtime_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_source_sha256: Digest
    chunker_version: Literal["v1_safe_windows"] = "v1_safe_windows"
    lexical_version: Literal["pg-simple-weighted-v1"] = "pg-simple-weighted-v1"
    selection_policy: Literal["admitted-path-order-v1"] = "admitted-path-order-v1"
    tokenizer_name: Literal["cl100k_base"] = "cl100k_base"
    tokenizer_sha256: Digest = TOKENIZER_SHA256
    target_tokens: Literal[768] = 768
    max_tokens: Literal[1024] = 1024
    max_bytes: Literal[4096] = 4096
    overlap_tokens: Literal[64] = 64
    max_chunks: int = Field(default=4096, ge=4096, le=4096)
    privacy_policy: IndexPrivacyPolicyV1 = Field(default_factory=IndexPrivacyPolicyV1)

    @model_validator(mode="after")
    def qualified(self) -> Self:
        if self.tokenizer_sha256 != TOKENIZER_SHA256:
            raise ValueError("unqualified tokenizer vocabulary")
        if self.privacy_policy.model_dump() != PrivacyPolicyV1().model_dump():
            raise ValueError("unqualified index privacy policy")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())


class IndexFileV1(Contract):
    file_id: UUID
    path: str = Field(min_length=1, max_length=1024)
    sha256: Digest
    size_bytes: int = Field(ge=0, le=2_097_152)

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        if validate_archive_path(self.path) != self.path:
            raise ValueError("file path cannot be a directory")
        return self


class IndexOmissionV1(Contract):
    reason: OmissionReason
    unit: Literal["file", "chunk"]
    count: int = Field(gt=0, le=1_000_000)


def _omissions(items: tuple[IndexOmissionV1, ...]) -> tuple[IndexOmissionV1, ...]:
    if len({(item.reason, item.unit) for item in items}) != len(items):
        raise ValueError("duplicate omission category")
    return tuple(sorted(items, key=lambda item: (item.unit, item.reason)))


class IndexSpecV1(Contract):
    schema_version: Literal[1] = 1
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID
    config: IndexBuildConfigV1
    source: ObjectDescriptor
    manifest: ObjectDescriptor
    strip_root: bool = False
    files: tuple[IndexFileV1, ...] = Field(max_length=128)
    admitted_files: int = Field(ge=0, le=10_000)
    selection_omissions: tuple[IndexOmissionV1, ...] = Field(default=(), max_length=9)

    @model_validator(mode="after")
    def selection(self) -> Self:
        if (
            len({item.file_id for item in self.files}) != len(self.files)
            or len({collision_key(item.path) for item in self.files}) != len(self.files)
            or sum(item.size_bytes for item in self.files) > 16_777_216
        ):
            raise ValueError("invalid index source selection")
        omissions = _omissions(self.selection_omissions)
        if any(item.unit != "file" for item in omissions) or (
            len(self.files) + sum(item.count for item in omissions) != self.admitted_files
        ):
            raise ValueError("incomplete source selection coverage")
        object.__setattr__(
            self, "files", tuple(sorted(self.files, key=lambda f: (f.path, f.file_id)))
        )
        object.__setattr__(self, "selection_omissions", omissions)
        if len(canonical_bytes(self.model_dump())) > 98_304:
            raise ValueError("index specification exceeds bound")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())


class IndexIntervalV1(Contract):
    """Half-open offsets relative to a chunk's safe and original envelopes."""

    safe_start_byte: int = Field(ge=0)
    safe_end_byte: int = Field(gt=0)
    original_start_byte: int = Field(ge=0)
    original_end_byte: int = Field(gt=0)
    is_redacted: bool = False

    @model_validator(mode="after")
    def ordered(self) -> Self:
        safe = self.safe_end_byte - self.safe_start_byte
        original = self.original_end_byte - self.original_start_byte
        if safe <= 0 or original <= 0 or (not self.is_redacted and safe != original):
            raise ValueError("invalid index interval")
        return self


class MaterializedChunkV1(Contract):
    file_id: UUID
    file_path: str = Field(min_length=1, max_length=1024)
    chunk_index: int = Field(ge=0, lt=4096)
    chunk_kind: Literal["file", "window"]
    start_byte: int = Field(ge=0)
    end_byte: int = Field(gt=0, le=2_097_152)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    safe_start_byte: int = Field(ge=0)
    safe_end_byte: int = Field(gt=0)
    content_sha256: Digest
    safe_content: str = Field(min_length=1, max_length=4096, repr=False)
    safe_content_sha256: Digest
    token_count: int = Field(ge=1, le=1024)
    redaction_digest: Digest
    mappings: tuple[IndexIntervalV1, ...] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def provenance(self) -> Self:
        safe = self.safe_content.encode("utf-8")
        if (
            len(safe) > 4096
            or self.safe_end_byte - self.safe_start_byte != len(safe)
            or self.end_byte <= self.start_byte
            or self.end_line < self.start_line
            or hashlib.sha256(safe).hexdigest() != self.safe_content_sha256
        ):
            raise ValueError("invalid materialized chunk content or bounds")
        if not any(item.is_redacted for item in self.mappings) and (
            self.content_sha256 != self.safe_content_sha256
        ):
            raise ValueError("unredacted original digest differs")
        safe_offset = original_offset = 0
        for item in self.mappings:
            if item.safe_start_byte != safe_offset or item.original_start_byte != original_offset:
                raise ValueError("noncontiguous chunk provenance")
            try:
                safe[item.safe_start_byte : item.safe_end_byte].decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("chunk interval splits UTF-8 codepoint") from None
            safe_offset, original_offset = item.safe_end_byte, item.original_end_byte
        if safe_offset != len(safe) or original_offset != self.end_byte - self.start_byte:
            raise ValueError("incomplete chunk provenance")
        payload = [item.model_dump() for item in self.mappings]
        if (
            semantic_digest(payload) != self.redaction_digest
            or len(canonical_bytes(payload)) > 65536
        ):
            raise ValueError("invalid redaction provenance digest or bound")
        return self


class IndexOmissionExampleV1(Contract):
    file_id: UUID
    file_path: str = Field(min_length=1, max_length=1024)
    reason: OmissionReason
    unit: Literal["file", "chunk"]

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        if validate_archive_path(self.file_path) != self.file_path:
            raise ValueError("invalid omission file path")
        return self


class IndexCoverageV1(Contract):
    admitted_files: int = Field(ge=0, le=10_000)
    selected_files: int = Field(ge=0, le=128)
    processed_files: int = Field(ge=0, le=128)
    omissions: tuple[IndexOmissionV1, ...] = Field(default=(), max_length=18)

    omission_examples: tuple[IndexOmissionExampleV1, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def accounting(self) -> Self:
        items = _omissions(self.omissions)
        if (
            not self.processed_files <= self.selected_files <= self.admitted_files
            or sum(item.count for item in items if item.unit == "file")
            != self.admitted_files - self.processed_files
        ):
            raise ValueError("inconsistent index coverage")
        keys = {(item.reason, item.unit) for item in items}
        examples = self.omission_examples
        if len({(item.file_id, item.reason, item.unit) for item in examples}) != len(
            examples
        ) or any((item.reason, item.unit) not in keys for item in examples):
            raise ValueError("inconsistent omission examples")
        object.__setattr__(
            self,
            "omission_examples",
            tuple(
                sorted(
                    examples,
                    key=lambda item: (item.file_path, item.file_id, item.unit, item.reason),
                )
            ),
        )
        object.__setattr__(self, "omissions", items)
        return self

    @property
    def status(self) -> Literal["complete", "partial"]:
        return "partial" if self.omissions else "complete"


class IndexPublicationDraftV1(Contract):
    schema_version: Literal[1] = 1
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    spec_hash: Digest
    config_hash: Digest
    chunks: tuple[MaterializedChunkV1, ...] = Field(max_length=4096, repr=False)
    processed_file_ids: tuple[UUID, ...] = Field(max_length=128)
    coverage: IndexCoverageV1

    @model_validator(mode="after")
    def bounded(self) -> Self:
        processed = set(self.processed_file_ids)
        if (
            len(processed) != len(self.processed_file_ids)
            or len(processed) != self.coverage.processed_files
        ):
            raise ValueError("inconsistent processed file identities")
        if any(item.file_id not in processed for item in self.chunks):
            raise ValueError("chunk belongs to unprocessed file")
        chunks = tuple(sorted(self.chunks, key=lambda c: (c.file_path, c.file_id, c.chunk_index)))
        ordinals: dict[UUID, int] = {}
        for item in chunks:
            expected = ordinals.get(item.file_id, 0)
            if item.chunk_index != expected:
                raise ValueError("duplicate or noncontiguous chunk ordinal")
            ordinals[item.file_id] = expected + 1
        if sum(len(item.safe_content.encode()) for item in chunks) > 16_777_216:
            raise ValueError("safe content aggregate exceeds bound")
        object.__setattr__(self, "chunks", chunks)
        object.__setattr__(self, "processed_file_ids", tuple(sorted(processed)))
        if len(canonical_bytes(self.manifest_payload())) > 8_388_608:
            raise ValueError("index manifest exceeds bound")
        if len(canonical_bytes(self.model_dump())) > 25_165_824:
            raise ValueError("index draft exceeds bound")
        return self

    def validate_for(self, spec: IndexSpecV1) -> None:
        if (
            (self.org_id, self.repository_id, self.snapshot_id)
            != (spec.org_id, spec.repository_id, spec.snapshot_id)
            or self.spec_hash != spec.semantic_hash()
            or self.config_hash != spec.config.semantic_hash()
            or self.coverage.admitted_files != spec.admitted_files
            or self.coverage.selected_files != len(spec.files)
        ):
            raise ValueError("index publication scope or specification differs")
        files = {item.file_id: item for item in spec.files}
        if not set(self.processed_file_ids) <= files.keys():
            raise ValueError("processed file outside selection")
        for example in self.coverage.omission_examples:
            source = files.get(example.file_id)
            if source is None or example.file_path != source.path:
                raise ValueError("omission example outside selected file identity")
        counts = {(item.reason, item.unit): item.count for item in self.coverage.omissions}
        if any(
            counts.get((item.reason, item.unit), 0) < item.count
            for item in spec.selection_omissions
        ):
            raise ValueError("selection omissions missing from publication")
        for chunk in self.chunks:
            source = files.get(chunk.file_id)
            if (
                source is None
                or chunk.file_path != source.path
                or chunk.end_byte > source.size_bytes
            ):
                raise ValueError("materialized file metadata differs")
            if (
                chunk.start_byte == 0
                and chunk.end_byte == source.size_bytes
                and chunk.content_sha256 != source.sha256
            ):
                raise ValueError("whole-file digest differs from admitted source")

    def manifest_payload(self) -> dict[str, object]:
        return {
            "org_id": self.org_id,
            "repository_id": self.repository_id,
            "snapshot_id": self.snapshot_id,
            "config_hash": self.config_hash,
            "chunks": [item.model_dump(exclude={"safe_content"}) for item in self.chunks],
            "processed_file_ids": self.processed_file_ids,
            "coverage": self.coverage.model_dump(),
        }

    def materialization_hash(self) -> str:
        return semantic_digest(self.manifest_payload())

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())

    def chunk_set_id(self) -> UUID:
        return uuid5(INDEX_NAMESPACE, self.materialization_hash())

    def lexical_index_id(self) -> UUID:
        return uuid5(self.chunk_set_id(), "lexical-v1")

    def stored_chunk_id(self, chunk: MaterializedChunkV1) -> UUID:
        if chunk not in self.chunks:
            raise ValueError("chunk is not in publication")
        return uuid5(
            self.chunk_set_id(), semantic_digest(chunk.model_dump(exclude={"safe_content"}))
        )


class EnqueueIndexJobV5(Contract):
    repository_id: UUID
    snapshot_id: UUID
    index_spec_id: UUID
    config_hash: Digest
    operation: Literal["index_snapshot"] = "index_snapshot"
    request_key: str = Field(min_length=1, max_length=128)
    retry_of_job_id: UUID | None = None

    @model_validator(mode="after")
    def request_identity(self) -> Self:
        if len(self.request_key.encode()) > 128 or self.request_key.startswith(("job:", "child:")):
            raise ValueError("invalid index request key")
        return self


class JobEnvelopeV5(Contract):
    schema_version: Literal[5] = 5
    org_id: UUID
    job_id: UUID
    index_spec_id: UUID
    operation: Literal["index_snapshot"] = "index_snapshot"
    config_hash: Digest
    dispatch_generation: int = Field(gt=0)
    deadline: AwareDatetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > 4096:
            raise ValueError("queue envelope exceeds bound")
        return self


class IndexJobRecordV5(Contract):
    schema_version: Literal[5] = 5
    org_id: UUID
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    index_spec_id: UUID
    operation: Literal["index_snapshot"] = "index_snapshot"
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
            raise ValueError("inconsistent index job state")
        return self


class IndexReceiptV1(Contract):
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    job_id: UUID
    attempt_id: UUID
    fence: int = Field(gt=0)
    index_spec_id: UUID
    spec_hash: Digest
    config_hash: Digest
    chunk_set_id: UUID
    index_id: UUID
    draft_digest: Digest
    materialization_hash: Digest
    coverage: IndexCoverageV1
    created_at: AwareDatetime

    @model_validator(mode="after")
    def identities(self) -> Self:
        if self.chunk_set_id != uuid5(INDEX_NAMESPACE, self.materialization_hash):
            raise ValueError("index receipt materialization identity differs")
        if self.index_id != uuid5(self.chunk_set_id, "lexical-v1"):
            raise ValueError("index receipt lexical identity differs")
        return self


class IndexCreateRequestV1(Contract):
    config: IndexBuildConfigV1
    request_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def request_identity(self) -> Self:
        if len(self.request_key.encode()) > 128 or self.request_key.startswith(("job:", "child:")):
            raise ValueError("invalid index request key")
        return self


class IndexCreateResponseV1(Contract):
    job: IndexJobRecordV5

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status_url(self) -> str:
        return (
            f"/api/v1/repositories/{self.job.repository_id}/snapshots/{self.job.snapshot_id}"
            f"/index-jobs/{self.job.id}"
        )


class IndexStatusV1(Contract):
    can_cancel: bool = False
    can_retry: bool = False
    job: IndexJobRecordV5
    receipt: IndexReceiptV1 | None = None

    @model_validator(mode="after")
    def published_result(self) -> Self:
        job, receipt = self.job, self.receipt
        if (job.state in {"succeeded", "partial"}) != (receipt is not None):
            raise ValueError("index status receipt availability differs")
        if receipt is not None and (
            (
                receipt.org_id,
                receipt.repository_id,
                receipt.snapshot_id,
                receipt.job_id,
                receipt.index_spec_id,
                receipt.index_id,
            )
            != (
                job.org_id,
                job.repository_id,
                job.snapshot_id,
                job.id,
                job.index_spec_id,
                job.result_index_id,
            )
            or (receipt.coverage.status == "complete") != (job.state == "succeeded")
        ):
            raise ValueError("index status receipt belongs to different publication")
        return self


class IndexRetryResponseV1(Contract):
    retry_job_id: UUID
    retry_of_job_id: UUID
    index_spec_id: UUID

    @model_validator(mode="after")
    def new_job(self) -> Self:
        if self.retry_job_id == self.retry_of_job_id:
            raise ValueError("retry must preserve original job history")
        return self


class IndexStartRequestV1(Contract):
    request_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def request_identity(self) -> Self:
        if len(self.request_key.encode()) > 128 or self.request_key.startswith(("job:", "child:")):
            raise ValueError("invalid index request key")
        return self


class IndexAvailabilityV1(Contract):
    repository_id: UUID
    snapshot_id: UUID
    chunk_set_id: UUID | None = None
    index_id: UUID | None = None
    config_hash: Digest | None = None
    materialization_hash: Digest | None = None
    coverage: IndexCoverageV1 | None = None
    lexical_availability: Literal["available", "index_unavailable"] = "index_unavailable"
    can_build: bool = False

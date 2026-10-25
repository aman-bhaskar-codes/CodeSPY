"""Deterministic safe windows and bounded coverage from verified admitted bytes."""

import hashlib
from collections import Counter
from collections.abc import Sequence
from typing import Literal, Self
from uuid import UUID

import tiktoken
from pydantic import Field, model_validator

from app.contracts.indexing import (
    IndexCoverageV1,
    IndexFileV1,
    IndexOmissionExampleV1,
    IndexOmissionV1,
    IndexPublicationDraftV1,
    IndexSpecV1,
    MaterializedChunkV1,
    OmissionReason,
)
from app.contracts.scanners import Digest
from app.domain.canonical_json import canonical_bytes
from app.domain.scope import Contract
from app.indexing.budget import HEADER_RESERVE, ChunkBudget
from app.indexing.mapping import (
    IndexMaterializationError,
    VerifiedTransform,
    safe_to_original_envelope,
)
from app.services.privacy_transform import is_sensitive_path, transform_model_safe

OmissionUnit = Literal["file", "chunk"]

__all__ = [
    "IndexMaterializationError",
    "assemble_draft",
    "materialize_file",
    "safe_to_original_envelope",
]


class MaterializedFileV1(Contract):
    spec_hash: Digest
    file: IndexFileV1
    processed: bool
    safe_file_sha256: Digest | None = None
    redaction_map_sha256: Digest | None = None
    chunks: tuple[MaterializedChunkV1, ...] = Field(default=(), max_length=4096, repr=False)
    omissions: tuple[IndexOmissionV1, ...] = Field(default=(), max_length=18)

    @model_validator(mode="after")
    def coverage(self) -> Self:
        if any(
            (digest is not None) != self.processed
            for digest in (self.safe_file_sha256, self.redaction_map_sha256)
        ):
            raise ValueError("file transformation identity differs")
        files = sum(item.count for item in self.omissions if item.unit == "file")
        if files != int(not self.processed) or (not self.processed and self.chunks):
            raise ValueError("inconsistent materialized file coverage")
        if any(
            item.file_id != self.file.file_id or item.file_path != self.file.path
            for item in self.chunks
        ):
            raise ValueError("materialized file differs")
        if self.processed and self.file.size_bytes and not self.chunks and not self.omissions:
            raise ValueError("nonempty file lost without omission")
        return self


def _tokens(encoder: tiktoken.Encoding, text: str) -> list[int]:
    try:
        return encoder.encode(text, disallowed_special=())
    except Exception:
        raise IndexMaterializationError("index_tokenizer_unavailable") from None


def _utf8_end(data: bytes, limit: int) -> int:
    end = min(limit, len(data))
    while end and end < len(data) and data[end] & 0xC0 == 0x80:
        end -= 1
    return end


def _candidate(
    view: VerifiedTransform, start: int, covered_end: int, encoder: tiktoken.Encoding
) -> tuple[int, int]:
    safe = view.output.safe_bytes
    end = _utf8_end(safe, start + 4096)
    values = _tokens(encoder, safe[start:end].decode())
    if len(values) > 768:
        end = start + len(encoder.decode_bytes(values[:768]))
        end = _utf8_end(safe, end)
    # Prefer a complete line where this does not split a placeholder.
    newline = safe.rfind(b"\n", start, end)
    if newline >= start and newline + 1 > covered_end:
        end = newline + 1
    return start, end


def _next_start(
    view: VerifiedTransform, start: int, end: int, values: list[int], encoder: tiktoken.Encoding
) -> int:
    if len(values) <= 64:
        return end
    safe = view.output.safe_bytes
    candidate = end - len(encoder.decode_bytes(values[-64:]))
    while candidate < end and safe[candidate] & 0xC0 == 0x80:
        candidate += 1
    index = view.containing(candidate)
    if index >= 0:
        mapping = view.output.mappings[index]
        if mapping.is_redacted and mapping.safe_start_byte < candidate < mapping.safe_end_byte:
            candidate = mapping.safe_end_byte
    if candidate <= start or len(_tokens(encoder, safe[candidate:end].decode())) > 64:
        return end
    return candidate


def materialize_file(
    spec: IndexSpecV1, file: IndexFileV1, raw: bytes, *, tokenizer: tiktoken.Encoding
) -> MaterializedFileV1:
    if (
        file not in spec.files
        or len(raw) != file.size_bytes
        or hashlib.sha256(raw).hexdigest() != file.sha256
    ):
        raise IndexMaterializationError("index_source_identity_differs")
    if tokenizer.name != spec.config.tokenizer_name:
        raise IndexMaterializationError("index_tokenizer_profile_differs")
    reason: OmissionReason | None = None
    if is_sensitive_path(file.path, spec.config.privacy_policy.sensitive_path_patterns):
        reason = "sensitive_path"
    else:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            reason = "unsupported_encoding"
    if reason is not None:
        return MaterializedFileV1(
            spec_hash=spec.semantic_hash(),
            file=file,
            processed=False,
            omissions=(IndexOmissionV1(reason=reason, unit="file", count=1),),
        )
    output = transform_model_safe(raw, spec.config.privacy_policy)
    view = VerifiedTransform.create(raw, output)
    safe = output.safe_bytes
    omissions: Counter[OmissionReason] = Counter()
    budget = ChunkBudget()
    start = covered_end = 0
    while start < len(safe):
        whole = len(safe) <= 4096 and len(_tokens(tokenizer, safe.decode())) <= 1024
        _, end = (0, len(safe)) if whole else _candidate(view, start, covered_end, tokenizer)
        if end <= start:
            raise IndexMaterializationError("index_window_no_progress")
        span = view.envelope(start, end)
        end = span.safe_end_byte
        covered_end = end
        content = safe[span.safe_start_byte : end].decode()
        values = _tokens(tokenizer, content)
        if end - span.safe_start_byte > 4096 or len(values) > 1024:
            omissions["placeholder_limit"] += 1
            start = end
            continue
        if (
            len(canonical_bytes([m.model_dump() for m in span.mappings])) > 65536
            or len(span.mappings) > 1024
        ):
            omissions["manifest_limit"] += 1
            start = end
            continue
        if len(budget.chunks) >= 4096:
            omissions["chunk_limit"] += 1
            start = end
            continue
        chunk = MaterializedChunkV1(
            file_id=file.file_id,
            file_path=file.path,
            chunk_index=len(budget.chunks),
            chunk_kind="file" if whole else "window",
            token_count=len(values),
            safe_content=content,
            **span.model_dump(),
        )
        rejected = budget.add(chunk)
        if rejected is not None:
            omissions[rejected] += 1
        if end == len(safe):
            break
        start = _next_start(view, start, end, values, tokenizer)
    return MaterializedFileV1(
        spec_hash=spec.semantic_hash(),
        file=file,
        processed=True,
        chunks=tuple(budget.chunks),
        safe_file_sha256=hashlib.sha256(safe).hexdigest(),
        redaction_map_sha256=output.redaction_map_digest,
        omissions=tuple(
            IndexOmissionV1(reason=key, unit="chunk", count=count)
            for key, count in sorted(omissions.items())
        ),
    )


def assemble_draft(
    spec: IndexSpecV1, files: Sequence[MaterializedFileV1]
) -> IndexPublicationDraftV1:
    selected = {file.file_id: file for file in spec.files}
    if len(files) != len(selected) or {item.file.file_id for item in files} != selected.keys():
        raise IndexMaterializationError("index_file_result_set_differs")
    omissions: Counter[tuple[OmissionReason, OmissionUnit]] = Counter()
    for value in spec.selection_omissions:
        omissions[value.reason, value.unit] += value.count
    examples: dict[tuple[UUID, OmissionReason, OmissionUnit], IndexOmissionExampleV1] = {}
    budget = ChunkBudget()
    processed = []
    per_file: Counter[UUID] = Counter()

    def record(file: IndexFileV1, reason: OmissionReason, unit: OmissionUnit, count: int) -> None:
        omissions[reason, unit] += count
        key = (file.file_id, reason, unit)
        if key not in examples and len(examples) < 128:
            examples[key] = IndexOmissionExampleV1(
                file_id=file.file_id, file_path=file.path, reason=reason, unit=unit
            )

    for item in sorted(files, key=lambda value: (value.file.path, value.file.file_id)):
        if item.spec_hash != spec.semantic_hash() or item.file != selected[item.file.file_id]:
            raise IndexMaterializationError("index_file_result_identity_differs")
        if item.processed:
            processed.append(item.file.file_id)
        for omission in item.omissions:
            record(item.file, omission.reason, omission.unit, omission.count)
        for chunk in item.chunks:
            chunk = chunk.model_copy(update={"chunk_index": per_file[item.file.file_id]})
            rejected = budget.add(chunk)
            if rejected is None:
                per_file[item.file.file_id] += 1
            else:
                record(item.file, rejected, "chunk", 1)
    coverage = IndexCoverageV1(
        admitted_files=spec.admitted_files,
        selected_files=len(selected),
        processed_files=len(processed),
        omissions=tuple(
            IndexOmissionV1(reason=key[0], unit=key[1], count=count)
            for key, count in sorted(omissions.items())
        ),
        omission_examples=tuple(examples.values()),
    )
    draft = IndexPublicationDraftV1(
        org_id=spec.org_id,
        repository_id=spec.repository_id,
        snapshot_id=spec.snapshot_id,
        spec_hash=spec.semantic_hash(),
        config_hash=spec.config.semantic_hash(),
        chunks=tuple(budget.chunks),
        processed_file_ids=tuple(processed),
        coverage=coverage,
    )
    if (
        len(canonical_bytes(draft.model_dump(exclude={"chunks"}))) > HEADER_RESERVE
        or len(
            canonical_bytes(
                {key: value for key, value in draft.manifest_payload().items() if key != "chunks"}
            )
        )
        > HEADER_RESERVE
    ):
        raise IndexMaterializationError("index_header_limit")
    draft.validate_for(spec)
    return draft

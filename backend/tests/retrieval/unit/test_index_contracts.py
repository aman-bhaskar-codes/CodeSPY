"""Offline indexing contracts; valid DTOs do not attest source or database authority."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.contracts.indexing import (
    EnqueueIndexJobV5,
    IndexBuildConfigV1,
    IndexCoverageV1,
    IndexFileV1,
    IndexIntervalV1,
    IndexJobRecordV5,
    IndexOmissionV1,
    IndexPublicationDraftV1,
    IndexSpecV1,
    JobEnvelopeV5,
    MaterializedChunkV1,
)
from app.contracts.ingestion import ObjectDescriptor
from app.contracts.jobs import JobEnvelopeV2, JobEnvelopeV3
from app.contracts.scan_jobs import JobEnvelopeV4
from app.domain.canonical_json import canonical_bytes, semantic_digest
from pydantic import ValidationError


def digest(raw=b"synthetic"):
    return hashlib.sha256(raw).hexdigest()


def config(**changes):
    return IndexBuildConfigV1(
        **{"runtime_image": "sha256:" + digest(), "runtime_source_sha256": digest(), **changes}
    )


def file(path="a.py", raw=b"print(1)\n"):
    return IndexFileV1(file_id=uuid4(), path=path, sha256=digest(raw), size_bytes=len(raw))


def spec(files=None, **changes):
    selected = files if files is not None else (file(),)
    return IndexSpecV1(
        **{
            "org_id": uuid4(),
            "repository_id": uuid4(),
            "snapshot_id": uuid4(),
            "principal_id": uuid4(),
            "config": config(),
            "source": ObjectDescriptor(key="source/fixture", sha256=digest(), size_bytes=32),
            "manifest": ObjectDescriptor(key="manifest/fixture", sha256=digest(), size_bytes=64),
            "files": selected,
            "admitted_files": len(selected),
            **changes,
        }
    )


def chunk(source_file, content="print(1)\n", **changes):
    size = len(content.encode())
    mappings = (
        IndexIntervalV1(
            safe_start_byte=0,
            safe_end_byte=size,
            original_start_byte=0,
            original_end_byte=size,
        ),
    )
    return MaterializedChunkV1(
        **{
            "file_id": source_file.file_id,
            "file_path": source_file.path,
            "chunk_index": 0,
            "chunk_kind": "file",
            "start_byte": 0,
            "end_byte": size,
            "start_line": 1,
            "end_line": 1,
            "safe_start_byte": 0,
            "safe_end_byte": size,
            "content_sha256": digest(content.encode()),
            "safe_content": content,
            "safe_content_sha256": digest(content.encode()),
            "token_count": 4,
            "mappings": mappings,
            "redaction_digest": semantic_digest(mappings_payload(mappings)),
            **changes,
        }
    )


def mappings_payload(mappings):
    return [item.model_dump() for item in mappings]


def draft(request=None, **changes):
    request = request or spec()
    return IndexPublicationDraftV1(
        **{
            "org_id": request.org_id,
            "repository_id": request.repository_id,
            "snapshot_id": request.snapshot_id,
            "spec_hash": request.semantic_hash(),
            "config_hash": request.config.semantic_hash(),
            "chunks": tuple(chunk(item) for item in request.files),
            "processed_file_ids": tuple(item.file_id for item in request.files),
            "coverage": IndexCoverageV1(
                admitted_files=request.admitted_files,
                selected_files=len(request.files),
                processed_files=len(request.files),
                omissions=request.selection_omissions,
            ),
            **changes,
        }
    )


def test_profile_is_exact_and_canonical():
    a, b = file("a.py"), file("b.py")
    request = spec((b, a))
    assert [item.path for item in request.files] == ["a.py", "b.py"]
    assert (
        request.semantic_hash()
        == spec(
            (a, b),
            **{
                name: getattr(request, name)
                for name in (
                    "org_id",
                    "repository_id",
                    "snapshot_id",
                    "principal_id",
                    "source",
                    "manifest",
                    "config",
                )
            },
        ).semantic_hash()
    )
    other = IndexSpecV1.model_validate({**request.model_dump(), "principal_id": uuid4()})
    assert other.semantic_hash() != request.semantic_hash()
    assert other.config.semantic_hash() == request.config.semantic_hash()
    first, second = draft(request), draft(other)
    assert first.materialization_hash() == second.materialization_hash()
    assert first.semantic_hash() != second.semantic_hash()
    assert first.chunk_set_id() == second.chunk_set_id()
    assert first.stored_chunk_id(first.chunks[0]) == second.stored_chunk_id(second.chunks[0])


@pytest.mark.parametrize(
    "changes",
    [
        {"target_tokens": 769},
        {"max_tokens": 8192},
        {"max_bytes": 4097},
        {"overlap_tokens": 63},
        {"tokenizer_name": "other"},
        {"tokenizer_sha256": "0" * 64},
        {"runtime_image": "latest"},
        {"privacy_policy": {"redact_secrets": False}},
        {"privacy_policy": {"allow_cloud_providers": True}},
        {"privacy_policy": {"unknown_option": True}},
        {"max_chunks": True},
    ],
)
def test_profile_rejects_unqualified_or_coerced_options(changes):
    with pytest.raises(ValidationError):
        config(**changes)


@pytest.mark.parametrize("path", ["../a.py", "/a.py", "a\\b.py", "a.py/", "x" * 1025])
def test_selection_rejects_unsafe_paths(path):
    with pytest.raises(ValidationError):
        file(path)


def test_selection_rejects_duplicate_identity_path_and_byte_overflow():
    item = file()
    for files in ((item, item), (item, file()), tuple(file(str(i)) for i in range(129))):
        with pytest.raises(ValidationError):
            spec(files)
    large = tuple(file(str(i), b"x" * 2_097_152) for i in range(9))
    with pytest.raises(ValidationError):
        spec(large)
    with pytest.raises(ValidationError):
        spec((item,), admitted_files=2)


@pytest.mark.parametrize(
    "changes",
    [
        {"safe_content_sha256": "0" * 64},
        {"safe_end_byte": 2},
        {"start_line": 2, "end_line": 1},
        {"end_byte": 0},
        {"token_count": 1025},
        {"token_count": True},
        {"redaction_digest": "0" * 64},
        {"safe_content": "é" * 2049},
        {"mappings": ()},
        {"raw_content": b"must not be stored"},
    ],
)
def test_chunk_rejects_invalid_safe_provenance(changes):
    with pytest.raises(ValidationError):
        chunk(file(), **changes)


def test_chunk_mapping_requires_contiguous_safe_and_original_coverage():
    wrong = (
        IndexIntervalV1(
            safe_start_byte=1, safe_end_byte=9, original_start_byte=1, original_end_byte=9
        ),
    )
    with pytest.raises(ValidationError):
        chunk(file(), mappings=wrong, redaction_digest=semantic_digest(mappings_payload(wrong)))


def test_draft_scope_metadata_and_duplicate_ordinal_are_verified():
    request = spec()
    good = draft(request)
    good.validate_for(request)
    for changes in (
        {"org_id": uuid4()},
        {"spec_hash": "0" * 64},
        {"config_hash": "0" * 64},
        {"chunks": (chunk(request.files[0], file_path="other.py"),)},
        {"chunks": (chunk(file()),)},
    ):
        with pytest.raises(ValueError):
            draft(request, **changes).validate_for(request)
    with pytest.raises(ValidationError):
        draft(request, chunks=(good.chunks[0], good.chunks[0]))
    with pytest.raises(ValidationError):
        draft(request, chunks=good.chunks * 4097)


def test_zero_chunk_output_distinguishes_empty_from_omitted_source():
    empty = spec(())
    result = draft(empty)
    assert result.coverage.status == "complete"
    omission = IndexOmissionV1(reason="sensitive_path", unit="file", count=1)
    request = spec((), admitted_files=1, selection_omissions=(omission,))
    coverage = IndexCoverageV1(
        admitted_files=1, selected_files=0, processed_files=0, omissions=(omission,)
    )
    result = draft(request, coverage=coverage)
    result.validate_for(request)
    assert result.coverage.status == "partial"
    with pytest.raises(ValidationError):
        IndexCoverageV1(admitted_files=1, selected_files=0, processed_files=0)
    with pytest.raises(ValidationError):
        IndexCoverageV1(admitted_files=1, selected_files=0, processed_files=1)


def test_selection_omissions_cannot_disappear_from_result():
    omission = IndexOmissionV1(reason="source_limit", unit="file", count=1)
    request = spec(admitted_files=2, selection_omissions=(omission,))
    changed = IndexOmissionV1(reason="unsupported_encoding", unit="file", count=1)
    result = draft(
        request,
        coverage=IndexCoverageV1(
            admitted_files=2, selected_files=1, processed_files=1, omissions=(changed,)
        ),
    )
    with pytest.raises(ValueError):
        result.validate_for(request)


def test_transport_is_strict_bounded_and_version_specific():
    now = datetime.now(UTC)
    payload = {
        "org_id": uuid4(),
        "job_id": uuid4(),
        "index_spec_id": uuid4(),
        "config_hash": digest(),
        "dispatch_generation": 1,
        "deadline": now + timedelta(minutes=15),
    }
    envelope = JobEnvelopeV5(**payload)
    raw = envelope.model_dump_json()
    assert len(canonical_bytes(envelope.model_dump())) <= 4096
    assert JobEnvelopeV5.model_validate_json(raw) == envelope
    for old in (JobEnvelopeV2, JobEnvelopeV3, JobEnvelopeV4):
        with pytest.raises(ValidationError):
            old.model_validate_json(raw)
    request = {
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "index_spec_id": uuid4(),
        "config_hash": digest(),
        "request_key": "owner-request",
    }
    assert EnqueueIndexJobV5(**request).operation == "index_snapshot"
    for key in ("job:x", "child:x", "é" * 65, ""):
        with pytest.raises(ValidationError):
            EnqueueIndexJobV5(**{**request, "request_key": key})
    with pytest.raises(ValidationError):
        JobEnvelopeV5(**{**payload, "dispatch_generation": True})
    with pytest.raises(ValidationError):
        JobEnvelopeV5(**{**payload, "result_run_id": uuid4()})


def test_status_requires_real_terminal_result_identity():
    now = datetime.now(UTC)
    payload = {
        "org_id": uuid4(),
        "id": uuid4(),
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "index_spec_id": uuid4(),
        "state": "queued",
        "attempt_count": 0,
        "max_attempts": 3,
        "deadline": now + timedelta(minutes=15),
        "failure_code": None,
        "result_index_id": None,
        "created_at": now,
        "finished_at": None,
    }
    assert IndexJobRecordV5(**payload).result_index_id is None
    with pytest.raises(ValidationError):
        IndexJobRecordV5(**{**payload, "state": "succeeded", "finished_at": now})
    with pytest.raises(ValidationError):
        IndexJobRecordV5(**{**payload, "result_index_id": uuid4()})


def test_public_contracts_bind_status_urls_and_receipt_identity():
    from app.contracts.indexing import (
        IndexCreateRequestV1,
        IndexCreateResponseV1,
        IndexReceiptV1,
        IndexRetryResponseV1,
        IndexStatusV1,
    )

    now = datetime.now(UTC)
    request = spec()
    result = draft(request)
    job_id, attempt_id, spec_id = uuid4(), uuid4(), uuid4()
    receipt = IndexReceiptV1(
        org_id=request.org_id,
        repository_id=request.repository_id,
        snapshot_id=request.snapshot_id,
        job_id=job_id,
        attempt_id=attempt_id,
        fence=1,
        index_spec_id=spec_id,
        spec_hash=request.semantic_hash(),
        config_hash=request.config.semantic_hash(),
        chunk_set_id=result.chunk_set_id(),
        index_id=result.lexical_index_id(),
        draft_digest=result.semantic_hash(),
        materialization_hash=result.materialization_hash(),
        coverage=result.coverage,
        created_at=now,
    )
    payload = {
        "org_id": request.org_id,
        "id": job_id,
        "repository_id": request.repository_id,
        "snapshot_id": request.snapshot_id,
        "index_spec_id": spec_id,
        "state": "succeeded",
        "attempt_count": 1,
        "max_attempts": 3,
        "deadline": now + timedelta(minutes=15),
        "failure_code": None,
        "result_index_id": receipt.index_id,
        "created_at": now,
        "finished_at": now,
    }
    job = IndexJobRecordV5(**payload)
    status = IndexStatusV1(job=job, receipt=receipt)
    assert status.receipt == receipt
    created = IndexCreateResponseV1(job=job)
    assert created.status_url == (
        f"/api/v1/repositories/{request.repository_id}/snapshots/{request.snapshot_id}"
        f"/index-jobs/{job_id}"
    )
    IndexCreateRequestV1(config=request.config, request_key="requested")
    IndexRetryResponseV1(retry_job_id=uuid4(), retry_of_job_id=job_id, index_spec_id=spec_id)
    with pytest.raises(ValidationError):
        IndexCreateRequestV1(config=request.config, request_key="job:reserved")
    with pytest.raises(ValidationError):
        IndexCreateResponseV1(job=job, status_url="https://external.invalid")
    with pytest.raises(ValidationError):
        IndexStatusV1(job=job, receipt=None)
    with pytest.raises(ValidationError):
        IndexStatusV1(
            job=job,
            receipt=IndexReceiptV1.model_validate({**receipt.model_dump(), "job_id": uuid4()}),
        )
    with pytest.raises(ValidationError):
        IndexReceiptV1.model_validate({**receipt.model_dump(), "index_id": uuid4()})


def test_large_escaped_payload_and_manifest_are_bounded():
    request = spec((file(raw=b"a" * 2_097_152),))
    source = request.files[0]
    values = tuple(chunk(source, content="\x01" * 4096, chunk_index=i) for i in range(1100))
    with pytest.raises(ValidationError, match="draft exceeds bound"):
        draft(request, chunks=values)
    one_byte = IndexIntervalV1(
        safe_start_byte=0, safe_end_byte=1, original_start_byte=0, original_end_byte=1
    )
    many = tuple(
        IndexIntervalV1.model_validate(
            {
                **one_byte.model_dump(),
                "safe_start_byte": i,
                "safe_end_byte": i + 1,
                "original_start_byte": i,
                "original_end_byte": i + 1,
            }
        )
        for i in range(400)
    )
    values = tuple(
        chunk(
            source,
            content="x" * 400,
            chunk_index=i,
            mappings=many,
            redaction_digest=semantic_digest(mappings_payload(many)),
        )
        for i in range(230)
    )
    with pytest.raises(ValidationError, match="manifest exceeds bound"):
        draft(request, chunks=values)


def test_unredacted_digest_and_utf8_interval_boundaries_are_checked():
    item = file(raw="é".encode())
    with pytest.raises(ValidationError):
        chunk(item, content="é", content_sha256="0" * 64)
    mappings = (
        IndexIntervalV1(
            safe_start_byte=0, safe_end_byte=1, original_start_byte=0, original_end_byte=1
        ),
        IndexIntervalV1(
            safe_start_byte=1, safe_end_byte=2, original_start_byte=1, original_end_byte=2
        ),
    )
    with pytest.raises(ValidationError):
        chunk(
            item,
            content="é",
            mappings=mappings,
            redaction_digest=semantic_digest(mappings_payload(mappings)),
        )


def test_whole_unredacted_file_digest_matches_admitted_file():
    request = spec((file(raw=b"different"),))
    with pytest.raises(ValueError):
        draft(request).validate_for(request)

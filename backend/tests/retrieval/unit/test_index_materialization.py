"""Real offline tokenizer and privacy transform; no isolation or provider claims."""

import hashlib
import itertools
import json
from dataclasses import replace
from uuid import uuid4

import pytest
import tiktoken
from app.contracts.indexing import IndexFileV1, IndexSpecV1
from app.contracts.source_view import IntervalMappingV1, PrivacyPolicyV1
from app.indexing.materialize import (
    IndexMaterializationError,
    assemble_draft,
    materialize_file,
    safe_to_original_envelope,
)
from app.services.privacy_transform import TransformOutput, transform_model_safe
from tests.retrieval.unit.test_index_contracts import spec


@pytest.fixture(scope="module")
def tokenizer():
    # Qualification supplies the hash-verified vocabulary cache explicitly.
    return tiktoken.get_encoding("cl100k_base")


def request(raw, path="src/a.py"):
    file = IndexFileV1(
        file_id=uuid4(), path=path, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
    )
    return spec((file,)), file


def test_short_file_is_verified_and_deterministic_across_request_principals(tokenizer):
    raw = b"print(1)\n"
    job, file = request(raw)
    first = materialize_file(job, file, raw, tokenizer=tokenizer)
    second_job = IndexSpecV1.model_validate({**job.model_dump(), "principal_id": uuid4()})
    second = materialize_file(second_job, file, raw, tokenizer=tokenizer)
    a, b = assemble_draft(job, (first,)), assemble_draft(second_job, (second,))
    assert len(a.chunks) == 1 and a.chunks[0].chunk_kind == "file"
    assert a.chunks[0].content_sha256 == file.sha256
    assert a.chunks[0].safe_content == raw.decode()
    assert a.chunks[0].token_count == len(tokenizer.encode(raw.decode()))
    assert a.chunk_set_id() == b.chunk_set_id()
    assert a.semantic_hash() != b.semantic_hash()
    assert a.coverage.status == "complete"
    assert "raw_content" not in a.model_dump_json()


@pytest.mark.parametrize("secret", ["codespy_sec_canary_short", "codespy_sec_canary_" + "x" * 150])
def test_expanding_and_shrinking_redactions_map_both_endpoints(secret, tokenizer):
    raw = ("before\n" + secret + "\nafter\n").encode()
    job, file = request(raw)
    output = transform_model_safe(raw, PrivacyPolicyV1())
    redaction = next(m for m in output.mappings if m.is_redacted)
    mapped = safe_to_original_envelope(
        raw, output, redaction.safe_start_byte + 1, redaction.safe_end_byte - 1
    )
    assert mapped.start_byte == len(b"before\n")
    assert mapped.end_byte == len(b"before\n") + len(secret.encode())
    assert mapped.start_line == mapped.end_line == 2
    assert mapped.safe_start_byte == redaction.safe_start_byte
    assert mapped.safe_end_byte == redaction.safe_end_byte
    assert mapped.mappings[0].original_start_byte == 0
    assert mapped.mappings[0].original_end_byte == len(secret.encode())
    result = assemble_draft(job, (materialize_file(job, file, raw, tokenizer=tokenizer),))
    assert secret not in result.model_dump_json()
    assert result.chunks[0].end_byte == len(raw)
    assert result.chunks[0].end_line == 3


def test_multiline_secret_preserves_original_citation_lines(tokenizer):
    key_hdr = b"-----" + b"BEGIN " + b"PRIVATE " + b"KEY" + b"-----\n"
    key_ftr = b"-----" + b"END " + b"PRIVATE " + b"KEY" + b"-----"
    raw = b"head\n" + key_hdr + b"synthetic canary\n" + key_ftr + b"\ntail\n"
    job, file = request(raw)
    output = transform_model_safe(raw, PrivacyPolicyV1())
    hidden = next(m for m in output.mappings if m.is_redacted)
    mapped = safe_to_original_envelope(raw, output, hidden.safe_start_byte, hidden.safe_end_byte)
    assert (mapped.start_line, mapped.end_line) == (2, 4)
    assert raw[mapped.start_byte : mapped.end_byte].endswith(key_ftr)
    result = materialize_file(job, file, raw, tokenizer=tokenizer)
    assert "synthetic canary" not in result.model_dump_json()
    assert result.chunks[0].end_line == 5


@pytest.mark.parametrize(
    "raw,path,reason",
    [(b"token=fixture\n", ".env", "sensitive_path"), (b"\xff\x80", "a.py", "unsupported_encoding")],
)
def test_omitted_files_never_become_clean_absence(raw, path, reason, tokenizer):
    job, file = request(raw, path)
    value = materialize_file(job, file, raw, tokenizer=tokenizer)
    assert not value.processed and not value.chunks
    draft = assemble_draft(job, (value,))
    assert draft.coverage.status == "partial" and not draft.processed_file_ids
    assert draft.coverage.omissions[0].reason == reason
    assert draft.coverage.omission_examples[0].file_path == path


def test_empty_file_is_complete_without_fabricated_chunk(tokenizer):
    job, file = request(b"")
    value = materialize_file(job, file, b"", tokenizer=tokenizer)
    draft = assemble_draft(job, (value,))
    assert value.processed and not draft.chunks and draft.coverage.status == "complete"
    assert draft.processed_file_ids == (file.file_id,)


@pytest.mark.parametrize("change", ["hash", "size", "foreign_file", "wrong_encoder"])
def test_input_integrity_failure_is_not_partial_success(change, tokenizer):
    raw = b"print(1)\n"
    job, file = request(raw)
    if change == "hash":
        raw = b"print(2)\n"
    elif change == "size":
        raw += b"x"
    elif change == "foreign_file":
        file = IndexFileV1.model_validate({**file.model_dump(), "file_id": uuid4()})
    if change == "wrong_encoder":
        from unittest.mock import Mock

        tokenizer = Mock(name="unqualified tokenizer", spec=tiktoken.Encoding)
        tokenizer.name = "other"
    with pytest.raises(IndexMaterializationError):
        materialize_file(job, file, raw, tokenizer=tokenizer)


def test_utf8_windows_are_bounded_cover_all_source_and_advance(tokenizer):
    raw = ("éé界🙂 A<|endoftext|>\n" * 700).encode()
    job, file = request(raw)
    output = materialize_file(job, file, raw, tokenizer=tokenizer)
    assert len(output.chunks) > 1
    previous_end = 0
    for i, chunk in enumerate(output.chunks):
        assert chunk.chunk_index == i
        assert len(chunk.safe_content.encode()) <= 4096
        assert (
            chunk.token_count
            == len(tokenizer.encode(chunk.safe_content, disallowed_special=()))
            <= 1024
        )
        assert chunk.start_byte <= previous_end and chunk.end_byte > previous_end
        assert raw[chunk.start_byte : chunk.end_byte].decode() == chunk.safe_content
        if i:
            overlap = raw[chunk.start_byte : previous_end].decode()
            assert len(tokenizer.encode(overlap, disallowed_special=())) <= 64
        previous_end = chunk.end_byte
    assert previous_end == len(raw)


def test_no_long_line_or_newline_boundary_drops_source(tokenizer):
    raw = ("x\n" + "abcdef9_" * 1500 + "\nlast").encode()
    job, file = request(raw)
    chunks = materialize_file(job, file, raw, tokenizer=tokenizer).chunks
    assert chunks[0].start_byte == 0 and chunks[-1].end_byte == len(raw)
    assert all(
        a.end_byte >= b.start_byte and b.end_byte > a.end_byte
        for a, b in itertools.pairwise(chunks)
    )


def test_redaction_crossing_window_edges_is_whole(tokenizer):
    secret = "codespy_sec_canary_" + "z" * 80
    raw = ("x " * 760 + secret + " trailing " * 800).encode()
    job, file = request(raw)
    output = transform_model_safe(raw, PrivacyPolicyV1())
    hidden = next(m for m in output.mappings if m.is_redacted)
    chunks = materialize_file(job, file, raw, tokenizer=tokenizer).chunks
    assert secret not in "".join(c.safe_content for c in chunks)
    for chunk in chunks:
        assert not hidden.safe_start_byte < chunk.safe_start_byte < hidden.safe_end_byte
        assert not hidden.safe_start_byte < chunk.safe_end_byte < hidden.safe_end_byte
    assert chunks[-1].end_byte == len(raw)


def fake_output(raw, replacement):
    mappings = (
        IntervalMappingV1(
            safe_start_byte=0,
            safe_end_byte=len(replacement),
            original_start_byte=0,
            original_end_byte=len(raw),
            is_redacted=True,
            redaction_type="fixture",
        ),
    )
    digest = hashlib.sha256(
        json.dumps(
            [m.model_dump() for m in mappings], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return TransformOutput(replacement, replacement.decode(), mappings, digest, 1)


def test_oversized_whole_placeholder_is_omitted_not_split(monkeypatch, tokenizer):
    # Synthetic exaggerated transform; the qualified default placeholder is much smaller.
    raw = b"abc"
    job, file = request(raw)
    output = fake_output(raw, b"x" * 5000)
    monkeypatch.setattr("app.indexing.materialize.transform_model_safe", lambda *_: output)
    value = materialize_file(job, file, raw, tokenizer=tokenizer)
    draft = assemble_draft(job, (value,))
    assert not draft.chunks and draft.coverage.status == "partial"
    assert draft.coverage.omissions[0].reason == "placeholder_limit"


def test_invalid_transform_mapping_is_rejected(monkeypatch, tokenizer):
    raw = b"abc"
    job, file = request(raw)
    output = fake_output(raw, b"replacement")
    broken = replace(
        output, mappings=(output.mappings[0].model_copy(update={"original_end_byte": 2}),)
    )
    monkeypatch.setattr("app.indexing.materialize.transform_model_safe", lambda *_: broken)
    with pytest.raises(IndexMaterializationError):
        materialize_file(job, file, raw, tokenizer=tokenizer)


def test_assembly_rejects_missing_duplicate_or_other_request_results(tokenizer):
    raw = b"x=1\n"
    job, file = request(raw)
    value = materialize_file(job, file, raw, tokenizer=tokenizer)
    other = IndexSpecV1.model_validate({**job.model_dump(), "principal_id": uuid4()})
    for items in ((), (value, value)):
        with pytest.raises(IndexMaterializationError):
            assemble_draft(job, items)
    with pytest.raises(IndexMaterializationError):
        assemble_draft(other, (value,))


def test_adjacent_redactions_and_clipped_identity_intervals():
    raw = b"abcd"
    maps = (
        IntervalMappingV1(
            safe_start_byte=0,
            safe_end_byte=5,
            original_start_byte=0,
            original_end_byte=2,
            is_redacted=True,
            redaction_type="fixture",
        ),
        IntervalMappingV1(
            safe_start_byte=5,
            safe_end_byte=11,
            original_start_byte=2,
            original_end_byte=4,
            is_redacted=True,
            redaction_type="fixture",
        ),
    )
    encoded = json.dumps(
        [item.model_dump() for item in maps], sort_keys=True, separators=(",", ":")
    ).encode()
    output = TransformOutput(
        b"firstsecond", "firstsecond", maps, hashlib.sha256(encoded).hexdigest(), 2
    )
    mapped = safe_to_original_envelope(raw, output, 2, 8)
    assert (mapped.safe_start_byte, mapped.safe_end_byte, mapped.start_byte, mapped.end_byte) == (
        0,
        11,
        0,
        4,
    )
    assert mapped.mappings[1].safe_start_byte == 5
    assert mapped.mappings[1].original_start_byte == 2
    raw = "éabc\ntail".encode()
    output = transform_model_safe(raw, PrivacyPolicyV1())
    clipped = safe_to_original_envelope(raw, output, 2, 5)
    assert clipped.start_byte == 2 and clipped.end_byte == 5
    assert clipped.mappings[0].safe_start_byte == clipped.mappings[0].original_start_byte == 0
    with pytest.raises(IndexMaterializationError):
        safe_to_original_envelope(raw, output, 1, 5)


def test_aggregate_row_cap_is_partial_deterministic_and_renumbers_retained_chunks():
    from app.indexing.materialize import MaterializedFileV1
    from tests.retrieval.unit.test_index_contracts import chunk

    raw = b"x" * 3072
    _, a = request(raw, "a.py")
    _, b = request(raw, "b.py")
    job = spec((b, a))
    values = []
    for item in (a, b):
        template = chunk(item, content="x", token_count=1, chunk_kind="window")
        rows = tuple(
            template.model_copy(
                update={
                    "chunk_index": i,
                    "start_byte": i,
                    "end_byte": i + 1,
                    "safe_start_byte": i,
                    "safe_end_byte": i + 1,
                }
            )
            for i in range(3072)
        )
        values.append(
            MaterializedFileV1(
                spec_hash=job.semantic_hash(),
                file=item,
                processed=True,
                chunks=rows,
                safe_file_sha256=item.sha256,
                redaction_map_sha256=hashlib.sha256(b"[]").hexdigest(),
            )
        )
    draft = assemble_draft(job, values)
    assert len(draft.chunks) == 4096 and draft.coverage.status == "partial"
    assert [(o.reason, o.count) for o in draft.coverage.omissions] == [("chunk_limit", 2048)]
    assert len(draft.coverage.omission_examples) == 1
    assert draft.coverage.omission_examples[0].file_id == b.file_id
    assert (
        draft.materialization_hash()
        == assemble_draft(job, tuple(reversed(values))).materialization_hash()
    )
    assert [c.chunk_index for c in draft.chunks if c.file_id == b.file_id] == list(range(1024))


@pytest.mark.parametrize("kind", ["manifest_limit", "draft_limit"])
def test_canonical_metadata_and_escaped_text_budgets_are_incremental(kind):
    from app.contracts.indexing import IndexIntervalV1
    from app.domain.canonical_json import semantic_digest
    from app.indexing.budget import HEADER_RESERVE, ChunkBudget
    from tests.retrieval.unit.test_index_contracts import chunk

    if kind == "manifest_limit":
        raw = b"x" * 400
        _, file = request(raw)
        maps = tuple(
            IndexIntervalV1(
                safe_start_byte=i,
                safe_end_byte=i + 1,
                original_start_byte=i,
                original_end_byte=i + 1,
            )
            for i in range(400)
        )
        template = chunk(
            file,
            content=raw.decode(),
            mappings=maps,
            redaction_digest=semantic_digest([m.model_dump() for m in maps]),
        )
    else:
        raw = b"\x01" * 4096
        _, file = request(raw)
        template = chunk(file, content=raw.decode())
    budget = ChunkBudget()
    reason = None
    for i in range(4096):
        reason = budget.add(template.model_copy(update={"chunk_index": i}))
        if reason is not None:
            break
    assert reason == kind
    assert budget.manifest_bytes + HEADER_RESERVE <= 8_388_608
    assert budget.draft_bytes + HEADER_RESERVE <= 25_165_824
    before = len(budget.chunks), budget.safe_bytes, budget.manifest_bytes, budget.draft_bytes
    assert budget.add(template) == kind
    assert before == (
        len(budget.chunks),
        budget.safe_bytes,
        budget.manifest_bytes,
        budget.draft_bytes,
    )


def test_omission_examples_cannot_reference_a_foreign_selected_file(tokenizer):
    from app.contracts.indexing import (
        IndexCoverageV1,
        IndexOmissionExampleV1,
        IndexOmissionV1,
        IndexPublicationDraftV1,
    )

    raw = b"\xff"
    job, file = request(raw)
    result = assemble_draft(job, (materialize_file(job, file, raw, tokenizer=tokenizer),))
    coverage = IndexCoverageV1(
        admitted_files=1,
        selected_files=1,
        processed_files=0,
        omissions=(IndexOmissionV1(reason="unsupported_encoding", unit="file", count=1),),
        omission_examples=(
            IndexOmissionExampleV1(
                file_id=uuid4(), file_path=file.path, reason="unsupported_encoding", unit="file"
            ),
        ),
    )
    changed = IndexPublicationDraftV1.model_validate({**result.model_dump(), "coverage": coverage})
    with pytest.raises(ValueError):
        changed.validate_for(job)


@pytest.mark.parametrize("field", ["safe_file_sha256", "redaction_map_sha256"])
def test_omitted_file_rejects_partial_transformation_identity(field):
    from app.indexing.materialize import MaterializedFileV1

    _, file = request(b"x")
    with pytest.raises(ValueError, match="transformation identity"):
        MaterializedFileV1.model_validate(
            {
                "spec_hash": "a" * 64,
                "file": file,
                "processed": False,
                field: "b" * 64,
                "omissions": ({"reason": "sensitive_path", "unit": "file", "count": 1},),
            }
        )


@pytest.mark.parametrize("prefix", ['"' * 100, "🙂" * 25], ids=["escaped", "unicode"])
def test_bounded_omission_header_accepts_escaped_and_unicode_paths(prefix, tokenizer):
    raw = b"\xff"
    files = tuple(
        IndexFileV1(
            file_id=uuid4(),
            path=f"{prefix}{i}.py",
            size_bytes=1,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        for i in range(128)
    )
    job = spec(files)
    draft = assemble_draft(
        job, tuple(materialize_file(job, f, raw, tokenizer=tokenizer) for f in files)
    )
    assert draft.coverage.status == "partial"
    assert len(draft.coverage.omission_examples) == 128
    assert draft.coverage.processed_files == 0

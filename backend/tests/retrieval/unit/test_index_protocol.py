"""Offline runtime protocol checks; host execution is not sandbox qualification."""

import hashlib
import os
from pathlib import Path

import pytest
from app.contracts.indexing import TOKENIZER_SHA256
from app.domain.canonical_json import canonical_bytes

from tests.retrieval.unit.test_index_contracts import file, spec


def stage(root, job, raw):
    (root / "request.json").write_bytes(
        canonical_bytes({"operation": "build", "spec": job.model_dump()})
    )
    (root / "archive.zip").write_bytes(raw)
    (root / "manifest.json").write_bytes(
        canonical_bytes(
            {"files": [{"file_id": str(job.files[0].file_id), "offset": 0, "size_bytes": len(raw)}]}
        )
    )


def test_runtime_build_checks_identity_and_returns_safe_draft(tmp_path):
    from app.contracts.indexing import IndexPublicationDraftV1
    from app.indexing.entrypoint import execute

    raw = b"codespy_sec_canary_runtime_123456789\n"
    job = spec((file(raw=raw),))
    stage(tmp_path, job, raw)
    output = execute(
        tmp_path, job.config.runtime_source_sha256, Path(os.environ["TIKTOKEN_CACHE_DIR"])
    )
    draft = IndexPublicationDraftV1.model_validate_json(output)
    draft.validate_for(job)
    assert raw.strip() not in output
    assert draft.chunks[0].content_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("fault", ["hash", "manifest", "symlink", "missing_vocab", "runtime"])
def test_runtime_rejects_invalid_private_input_without_source_diagnostics(tmp_path, fault):
    from app.indexing.entrypoint import execute

    raw = b"synthetic private input"
    job = spec((file(raw=raw),))
    stage(tmp_path, job, raw)
    cache = Path(os.environ["TIKTOKEN_CACHE_DIR"])
    identity = job.config.runtime_source_sha256
    if fault == "hash":
        (tmp_path / "archive.zip").write_bytes(b"x" * len(raw))
    elif fault == "manifest":
        (tmp_path / "manifest.json").write_bytes(b'{"files":[]}')
    elif fault == "symlink":
        (tmp_path / "archive.zip").unlink()
        (tmp_path / "archive.zip").symlink_to(tmp_path / "request.json")
    elif fault == "missing_vocab":
        cache = tmp_path / "missing"
    else:
        identity = "0" * 64
    with pytest.raises(ValueError):
        execute(tmp_path, identity, cache)


def test_capability_probe_requires_verified_pinned_vocabulary(tmp_path):
    from app.indexing.entrypoint import execute
    from app.ports.index_builder import IndexCapabilitiesV1

    (tmp_path / "request.json").write_bytes(b'{"operation":"capabilities"}')
    result = IndexCapabilitiesV1.model_validate_json(
        execute(tmp_path, "a" * 64, Path(os.environ["TIKTOKEN_CACHE_DIR"]))
    )
    assert result.runtime_source_sha256 == "a" * 64
    assert result.tokenizer_sha256 == TOKENIZER_SHA256
    assert result.max_source_bytes == 16_777_216
    assert result.max_output_bytes == 25_165_824
    with pytest.raises(ValueError):
        execute(tmp_path, "a" * 64, tmp_path / "missing")


def test_runtime_accepts_valid_spec_above_old_request_pipe_bound(tmp_path):
    from app.contracts.indexing import IndexPublicationDraftV1
    from app.indexing.entrypoint import execute

    files = tuple(file(path="x" * 500 + f"{i:03d}.py", raw=b"") for i in range(128))
    job = spec(files)
    request = canonical_bytes({"operation": "build", "spec": job.model_dump()})
    assert 66_000 < len(request) <= 98_368
    (tmp_path / "request.json").write_bytes(request)
    (tmp_path / "archive.zip").write_bytes(b"")
    (tmp_path / "manifest.json").write_bytes(
        canonical_bytes(
            {
                "files": [
                    {"file_id": str(f.file_id), "offset": 0, "size_bytes": 0} for f in job.files
                ]
            }
        )
    )
    result = IndexPublicationDraftV1.model_validate_json(
        execute(tmp_path, job.config.runtime_source_sha256, Path(os.environ["TIKTOKEN_CACHE_DIR"]))
    )
    assert len(result.processed_file_ids) == 128 and result.coverage.status == "complete"

"""Trusted build context excludes repository secrets and unlisted application code."""

import json
import os
from pathlib import Path

import pytest


def test_build_context_is_whitelisted_and_hash_locked(tmp_path):
    from scripts.build_indexer_image import prepare_context

    vocabulary = Path(os.environ["TIKTOKEN_CACHE_DIR"]) / "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
    digest = prepare_context(tmp_path, vocabulary)
    assert json.loads((tmp_path / "runtime.json").read_text()) == {"runtime_source_sha256": digest}
    assert len(digest) == 64
    assert not list(tmp_path.rglob(".env*"))
    assert not (tmp_path / "app/api").exists()
    assert not (tmp_path / "app/scanners/adapter.py").exists()
    assert (tmp_path / "app/indexing/entrypoint.py").is_file()
    assert (tmp_path / "vocabulary").read_bytes() == vocabulary.read_bytes()
    lines = (tmp_path / "requirements.txt").read_text().splitlines()
    assert lines and all("==" in line and "--hash=sha256:" in line for line in lines)
    second = tmp_path / "second"
    second.mkdir()
    assert prepare_context(second, vocabulary) == digest


def test_corrupt_vocabulary_cannot_enter_trusted_build(tmp_path):
    from scripts.build_indexer_image import prepare_context

    source = tmp_path / "vocab"
    source.write_bytes(b"corrupt")
    context = tmp_path / "build"
    context.mkdir()
    with pytest.raises(ValueError, match="vocabulary"):
        prepare_context(context, source)


def test_offline_wheelhouse_rejects_unlocked_files(tmp_path):
    from scripts.build_indexer_image import prepare_context

    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / "untrusted.whl").write_bytes(b"not locked")
    target = tmp_path / "context"
    target.mkdir()
    vocabulary = Path(os.environ["TIKTOKEN_CACHE_DIR"]) / "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
    with pytest.raises(ValueError, match="wheel"):
        prepare_context(target, vocabulary, wheelhouse=wheelhouse)

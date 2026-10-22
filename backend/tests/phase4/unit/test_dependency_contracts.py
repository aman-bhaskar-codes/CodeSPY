"""Dependency results bind exact selected inputs and evidence versions."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from app.contracts.dependencies import DependencyRequestV1, DependencyResultV1


def request(**changes):
    data = {
        "scope": {"org_id": UUID(int=1), "snapshot_id": UUID(int=2), "run_id": UUID(int=3)},
        "repository_id": UUID(int=4),
        "config_hash": "a" * 64,
        "runtime_digest": "d" * 64,
        "bundle_digest": "b" * 64,
        "evaluated_at": datetime(2026, 10, 21, tzinfo=UTC),
        "files": (
            {
                "file_id": UUID(int=5),
                "sha256": "c" * 64,
                "size_bytes": 100,
                "format": "package-lock-v3",
            },
        ),
    }
    data.update(changes)
    return DependencyRequestV1.model_validate(data)


def test_complete_requires_a_fresh_answer_for_every_package_instance():
    source = request()
    data = {
        "request_digest": source.semantic_hash(),
        "coverage": "complete",
        "files": ({"file_id": UUID(int=5), "status": "parsed"},),
        "packages": (
            {
                "file_id": UUID(int=5),
                "location": "node_modules/example",
                "query": {"ecosystem": "npm", "name": "example", "version": "1.2.3"},
                "assessment": {"status": "missing"},
            },
        ),
    }
    result = DependencyResultV1.model_validate(data)
    with pytest.raises(ValueError):
        result.validate_for(source)


def test_request_replay_and_duplicate_selected_metadata_are_rejected():
    source = request()
    with pytest.raises(ValueError):
        request(files=source.files * 2)
    result = DependencyResultV1(request_digest="f" * 64, coverage="unknown", files=(), packages=())
    with pytest.raises(ValueError):
        result.validate_for(source)


def test_metadata_capacity_is_distinct_from_source_capacity():
    with pytest.raises(ValueError):
        request(
            files=(
                {
                    "file_id": UUID(int=5),
                    "sha256": "c" * 64,
                    "size_bytes": 262145,
                    "format": "package-lock-v3",
                },
            )
        )

"""Regressions for the previously unqualified structural integration seams."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.api.application import ApiSettings
from app.contracts.structure import AnalysisConfigV1
from app.domain.errors import DuplicateConflict
from app.services.analysis_sources import source_file_query
from app.services.structural_analysis import replay_spec


def test_source_query_uses_snapshot_and_preserves_language():
    sql = str(source_file_query(uuid4(), uuid4()))
    assert "source_files.repository_id" not in sql
    assert "source_files.snapshot_id" in sql
    assert "source_files.language" in sql


def test_structural_api_is_disabled_until_toolchain_is_configured():
    assert not ApiSettings().structural_enabled
    with pytest.raises(ValueError):
        ApiSettings(structural_enabled=True)
    with pytest.raises(ValueError):
        ApiSettings(structural_enabled=True, structural_toolchain_hash="0" * 64)


async def test_replay_compares_stable_request_semantics():
    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    repo, snapshot = uuid4(), uuid4()
    existing = {
        "repository_id": repo,
        "snapshot_id": snapshot,
        "config_hash": config.semantic_hash(),
        "operation": "analyze_structure",
        "analysis_spec_id": uuid4(),
    }
    service = AsyncMock()
    service.get_spec.return_value = "persisted-spec"
    assert await replay_spec(service, None, existing, repo, snapshot, config) == "persisted-spec"
    with pytest.raises(DuplicateConflict):
        await replay_spec(service, None, existing, repo, uuid4(), config)


def test_stored_config_round_trip_preserves_json_semantics():
    import json

    from app.contracts.structure import stored_config

    config = AnalysisConfigV1(toolchain_hash="b" * 64, include_prefixes=("src",))
    assert stored_config(json.loads(config.model_dump_json())) == config

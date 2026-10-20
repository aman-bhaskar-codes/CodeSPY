"""Regressions for the previously unqualified structural integration seams."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.api.application import ApiSettings
from app.contracts.structure import AnalysisConfigV1
from app.domain.errors import DuplicateConflict, RetryRejected
from app.domain.scope import OrgRole, TenantContext
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
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.OWNER)
    existing = {
        "principal_id": ctx.principal_id,
        "repository_id": repo,
        "snapshot_id": snapshot,
        "config_hash": config.semantic_hash(),
        "operation": "analyze_structure",
        "analysis_spec_id": uuid4(),
    }
    service = AsyncMock()
    service.get_spec.return_value = "persisted-spec"
    assert await replay_spec(service, ctx, existing, repo, snapshot, config) == "persisted-spec"
    with pytest.raises(DuplicateConflict):
        await replay_spec(service, ctx, existing, repo, uuid4(), config)


def test_stored_config_round_trip_preserves_json_semantics():
    import json

    from app.contracts.structure import stored_config

    config = AnalysisConfigV1(toolchain_hash="b" * 64, include_prefixes=("src",))
    assert stored_config(json.loads(config.model_dump_json())) == config


async def test_request_key_cannot_replay_another_principals_spec():
    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    repo, snapshot = uuid4(), uuid4()
    existing = {
        "principal_id": uuid4(),
        "repository_id": repo,
        "snapshot_id": snapshot,
        "operation": "analyze_structure",
        "config_hash": config.semantic_hash(),
        "analysis_spec_id": uuid4(),
    }
    with pytest.raises(DuplicateConflict):
        await replay_spec(AsyncMock(), ctx, existing, repo, snapshot, config)


async def test_structural_retry_rejects_other_principal_before_enqueue(monkeypatch):
    import app.services.retries as module
    from app.services.retries import RetryService

    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    old = {
        "id": uuid4(),
        "principal_id": uuid4(),
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "state": "cancelled",
        "analysis_spec_id": uuid4(),
        "config_hash": "a" * 64,
    }
    monkeypatch.setattr(module, "lock_scope", AsyncMock())
    monkeypatch.setattr(module, "load_job", AsyncMock(return_value=old))
    session = AsyncMock()
    session.execute.side_effect = AssertionError("unauthorized retry reached database enqueue path")
    with pytest.raises(RetryRejected, match="retry_principal_mismatch"):
        await RetryService(None, None)._retry_structure(session, ctx, old, "new-key")

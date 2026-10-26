"""Ready derived identities are verified; non-ready records are never resurrected."""

import hashlib
from datetime import datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.contracts.source_view import PrivacyPolicyV1, SourceViewRequestV1
from app.models.derived_artifacts import derived_artifacts
from app.ports.source_reader import SourceReaderUnavailable
from app.services.privacy_transform import transform_model_safe
from app.services.source_view import SourceFileRow, SourceViewService

from tests.phase5.unit.test_source_view_service import MemoryObjectStore
from tests.phase5.unit.test_source_view_service import session as source_session_fixture

session = source_session_fixture


@pytest.mark.parametrize("status", ["tombstoned", "failed", "mismatched_ready", "expired"])
async def test_retained_non_ready_or_conflicting_registry_does_not_return_source(
    session, status, monkeypatch
):
    content = b"print(1)"
    digest = hashlib.sha256(content).hexdigest()
    request = SourceViewRequestV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        file_id=uuid4(),
    )
    transformed = transform_model_safe(content, PrivacyPolicyV1())
    monkeypatch.setattr(
        "app.services.source_view.database_now", AsyncMock(return_value=datetime(2026, 10, 24))
    )
    await session.execute(
        derived_artifacts.insert().values(
            org_id=request.org_id,
            id=uuid4(),
            repository_id=request.repository_id,
            snapshot_id=request.snapshot_id,
            file_id=request.file_id,
            representation="model_safe",
            policy_version="v1_default",
            source_sha256=digest,
            derived_sha256="0" * 64
            if status == "mismatched_ready"
            else hashlib.sha256(transformed.safe_bytes).hexdigest(),
            size_bytes=len(content),
            redaction_digest=transformed.redaction_map_digest,
            transformation_status="ready" if status in {"mismatched_ready", "expired"} else status,
            interval_map_json=[m.model_dump() for m in transformed.mappings],
            expires_at=datetime(2026, 1, 1) if status == "expired" else None,
            storage_key=None,
        )
    )
    await session.commit()
    service = SourceViewService(session, MemoryObjectStore({}))
    with pytest.raises(SourceReaderUnavailable):
        await service._build_model_safe_view(
            content,
            SourceFileRow(request.file_id, "main.py", digest, len(content)),
            request,
            PrivacyPolicyV1(),
        )
    await session.rollback()

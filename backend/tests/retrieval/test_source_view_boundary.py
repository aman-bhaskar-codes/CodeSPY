"""Service boundary tests with mocked authority and a controlled reader."""

import hashlib
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from app.contracts.ingestion import AdmissionPolicyV1, ObjectDescriptor
from app.contracts.source_view import PrivacyPolicyV1, SourceRepresentation, SourceViewRequestV1
from app.contracts.structure import SourceFileV1
from app.domain.errors import ScopeViolation
from app.ports.source_reader import SourceReaderUnavailable
from app.services.analysis_sources import LocatedSource
from app.services.source_view import SourceFileRow, SourceViewService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["ok", "revoked", "excluded", "unconfigured", "oversize", "wrong_bytes"]
)
async def test_source_view_uses_explicit_reader_and_rechecks_authority(scenario: str) -> None:
    raw = b"print(1)"
    digest = hashlib.sha256(raw).hexdigest()
    file_id = uuid4()
    request = SourceViewRequestV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        file_id=file_id,
        representation=SourceRepresentation.ORIGINAL,
    )
    selected = SourceFileV1(
        file_id=file_id,
        path="main.py",
        sha256=digest,
        size_bytes=2_097_153 if scenario == "oversize" else len(raw),
        eligible=scenario != "excluded",
        exclusion_reason="policy" if scenario == "excluded" else None,
    )
    descriptor = ObjectDescriptor(key="source/fixture", sha256="0" * 64, size_bytes=10)
    located = LocatedSource(descriptor, descriptor, (selected,), AdmissionPolicyV1(), None)
    reader = AsyncMock()
    reader.read.return_value = b"wrong" if scenario == "wrong_bytes" else raw
    service = SourceViewService(
        AsyncMock(), AsyncMock(), reader=None if scenario == "unconfigured" else reader
    )
    authority = AsyncMock(
        side_effect=[None, ScopeViolation("revoked")] if scenario == "revoked" else None
    )
    with (
        patch.object(service, "_verify_scope_and_representation", authority),
        patch.object(
            service,
            "_get_source_file",
            AsyncMock(return_value=SourceFileRow(file_id, "main.py", digest, selected.size_bytes)),
        ),
        patch("app.services.source_view.locate_source", AsyncMock(return_value=located)),
    ):
        if scenario == "ok":
            result = await service.get(request, PrivacyPolicyV1(allow_original_view=True))
            assert result.content_bytes == raw
            assert authority.await_count == 2
        else:
            expected = (
                SourceReaderUnavailable
                if scenario in {"unconfigured", "oversize", "wrong_bytes"}
                else ScopeViolation
            )
            with pytest.raises(expected):
                await service.get(request, PrivacyPolicyV1(allow_original_view=True))
        if scenario in {"excluded", "unconfigured", "oversize"}:
            reader.read.assert_not_awaited()

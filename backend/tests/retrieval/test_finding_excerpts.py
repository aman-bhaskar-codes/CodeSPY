"""Redaction mapping tests; original offsets must never index transformed bytes."""

from app.contracts.source_view import IntervalMappingV1
from app.services.finding_excerpts import redacted_span


def mapping(start, end, original_start, original_end, redacted=False):
    return IntervalMappingV1(
        safe_start_byte=start,
        safe_end_byte=end,
        original_start_byte=original_start,
        original_end_byte=original_end,
        is_redacted=redacted,
    )


def test_original_range_maps_through_longer_redaction() -> None:
    safe = b"abc[REDACTED]xyz"
    maps = (mapping(0, 3, 0, 3), mapping(3, 13, 3, 6, True), mapping(13, 16, 6, 9))
    assert redacted_span(safe, maps, 6, 9) == "xyz"
    assert redacted_span(safe, maps, 4, 8) == "[REDACTED]xy"


def test_missing_mapping_partial_content_and_large_span_are_unavailable() -> None:
    assert redacted_span(b"abc", (), 0, 3) is None
    assert redacted_span(b"ab", (mapping(0, 3, 0, 3),), 0, 3) is None
    raw = b"x" * 8193
    assert redacted_span(raw, (mapping(0, len(raw), 0, len(raw)),), 0, len(raw)) is None
    assert redacted_span(b"abc", (mapping(0, 3, 0, 3),), 1, 1) is None


def test_utf8_boundary_must_be_valid_and_markup_stays_plain_text() -> None:
    raw = "λ<script>alert(1)</script>".encode()
    maps = (mapping(0, len(raw), 0, len(raw)),)
    assert redacted_span(raw, maps, 0, 1) is None
    assert redacted_span(raw, maps, 2, len(raw)) == "<script>alert(1)</script>"


async def test_excerpt_orchestration_excludes_secrets_and_propagates_revocation(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    import pytest
    from app.domain.errors import ScopeViolation
    from app.domain.scope import OrgRole, TenantContext
    from app.services.finding_excerpts import FindingExcerptService
    from app.services.source_view import SourceViewService

    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    file_id, repository, snapshot = uuid4(), uuid4(), uuid4()
    get = AsyncMock(
        return_value=SimpleNamespace(
            is_omitted=False,
            file_sha256="a" * 64,
            actual_range=SimpleNamespace(start_byte=0),
            content_bytes=b"abc[REDACTED]xyz",
            interval_mappings=(
                mapping(0, 3, 0, 3),
                mapping(3, 13, 3, 6, True),
                mapping(13, 16, 6, 9),
            ),
        )
    )
    monkeypatch.setattr(SourceViewService, "get", get)
    service = FindingExcerptService(AsyncMock(), AsyncMock())
    payload = {
        "entity": {
            "kind": "source",
            "tool": "semgrep",
            "file_id": str(file_id),
            "sha256": "a" * 64,
            "range": {
                "start_byte": 6,
                "end_byte": 9,
                "start_line": 1,
                "end_line": 1,
                "start_col": 6,
                "end_col": 9,
            },
        }
    }
    secret = await service.read(
        AsyncMock(), ctx, repository, snapshot, file_id, "secret_format", payload
    )
    assert secret.text is None and secret.reason == "secret_format_excluded"
    get.assert_not_awaited()
    result = await service.read(
        AsyncMock(), ctx, repository, snapshot, file_id, "source_pattern", payload
    )
    assert result.text == "xyz" and result.reason == "available"
    assert get.await_args.args[0].representation.value == "model_safe"
    get.side_effect = ScopeViolation("revoked")
    with pytest.raises(ScopeViolation):
        await service.read(
            AsyncMock(), ctx, repository, snapshot, file_id, "source_pattern", payload
        )

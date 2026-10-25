"""Small model-safe finding spans; original byte offsets are mapped after redaction."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.findings import NativeFindingEntityV1
from app.contracts.source_view import IntervalMappingV1, PrivacyPolicyV1, SourceViewRequestV1
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import DuplicateConflict
from app.domain.scope import TenantContext
from app.ports.object_store import ObjectStore
from app.ports.source_reader import SourceReaderPort, SourceReaderUnavailable
from app.services.source_view import SourceViewService


def redacted_span(
    safe: bytes, mappings: Sequence[IntervalMappingV1], start: int, end: int
) -> str | None:
    """Return a completely mapped <=8 KiB span, or explicitly omit it."""
    if start < 0 or end <= start:
        return None
    covered = start
    pieces = []
    size = 0
    for item in mappings:
        lo, hi = max(start, item.original_start_byte), min(end, item.original_end_byte)
        if lo >= hi:
            continue
        if lo != covered:
            return None
        covered = hi
        if item.is_redacted:
            a, b = item.safe_start_byte, item.safe_end_byte
        else:
            if (
                item.safe_end_byte - item.safe_start_byte
                != item.original_end_byte - item.original_start_byte
            ):
                return None
            a = item.safe_start_byte + lo - item.original_start_byte
            b = item.safe_start_byte + hi - item.original_start_byte
        if not 0 <= a <= b <= len(safe):
            return None
        size += b - a
        if size > 8192:
            return None
        pieces.append(safe[a:b])
    if covered != end:
        return None
    try:
        return b"".join(pieces).decode("utf-8") or None
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True)
class FindingExcerpt:
    text: str | None
    reason: str


class FindingExcerptService:
    def __init__(self, store: ObjectStore | None, reader: SourceReaderPort | None):
        self.store, self.reader = store, reader

    async def read(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        file_id: UUID,
        category: str,
        payload: dict[str, Any],
    ) -> FindingExcerpt:
        if category == "secret_format":
            return FindingExcerpt(None, "secret_format_excluded")
        if category != "source_pattern":
            return FindingExcerpt(None, "unsupported_entity")
        if self.reader is None or self.store is None:
            return FindingExcerpt(None, "reader_unavailable")
        try:
            entity = NativeFindingEntityV1.model_validate_json(
                canonical_bytes(payload.get("entity"))
            )
            if entity.file_id != file_id:
                return FindingExcerpt(None, "source_unavailable")
            result = await SourceViewService(session, self.store, reader=self.reader).get(
                SourceViewRequestV1(
                    org_id=ctx.org_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    principal_id=ctx.principal_id,
                    file_id=file_id,
                ),
                PrivacyPolicyV1(max_view_bytes=4_194_304),
            )
        except (SourceReaderUnavailable, DuplicateConflict, NoResultFound, ValueError):
            return FindingExcerpt(None, "source_unavailable")
        # ScopeViolation is intentionally not suppressed: revoked authority fails the read.
        if result.is_omitted:
            return FindingExcerpt(None, "privacy_omitted")
        if result.file_sha256 != entity.sha256 or result.actual_range.start_byte != 0:
            return FindingExcerpt(None, "source_unavailable")
        text = redacted_span(
            result.content_bytes,
            result.interval_mappings,
            entity.range.start_byte,
            entity.range.end_byte,
        )
        return FindingExcerpt(text, "available" if text is not None else "range_unavailable")

"""Public finding details must not invent support or parse archives in the API."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from app.contracts.scan_publication import EffectiveVerificationV1
from app.models.scan_tables import finding_occurrences, verification_decisions
from app.services.findings_query import FindingsQueryService
from tests.phase4.unit.test_findings_authorization import authority as authority_fixture

authority = authority_fixture


@pytest.fixture
async def supported_finding(authority):
    async with authority.factory() as session, session.begin():
        await session.execute(
            verification_decisions.insert().values(
                org_id=authority.ctx.org_id,
                snapshot_id=authority.snapshot_id,
                run_id=authority.run_id,
                finding_id=authority.finding_id,
                disposition="supported-static",
                payload_json={},
                canonical_text="{}",
                payload_digest="a" * 64,
            )
        )
        await session.execute(
            sa.update(finding_occurrences).values(
                payload_json={
                    "title": "Synthetic static finding",
                    "severity": "high",
                    "entity": {"range": {"start_line": 1, "end_line": 1}},
                }
            )
        )
    return authority


@pytest.mark.parametrize(
    ("effective", "reason"),
    [
        ("supported-static", "current_support"),
        ("unresolved", "source_unavailable"),
        ("unresolved", "advisory_not_current"),
        ("unresolved", "bundle_unavailable"),
    ],
)
async def test_detail_projects_current_support_without_overwriting_history(
    supported_finding, monkeypatch, effective, reason
):
    from app.services.scan_reads import ScanReadService

    case = supported_finding

    async def read(_self, session, ctx, ref):
        assert ctx == case.ctx and ref.run_id == case.run_id
        return SimpleNamespace(
            effective_decisions=(
                EffectiveVerificationV1(
                    finding_id=case.finding_id,
                    historical_disposition="supported-static",
                    effective_disposition=effective,
                    reason=reason,
                ),
            )
        )

    monkeypatch.setattr(ScanReadService, "read_in_session", read, raising=False)
    detail = await FindingsQueryService(case.factory).get_finding_detail(
        case.ctx, case.repository_id, case.snapshot_id, case.finding_id
    )
    assert detail.disposition == effective
    assert detail.historical_disposition == "supported-static"
    assert detail.support_reason == reason
    async with case.factory() as session:
        assert (
            await session.scalar(sa.select(verification_decisions.c.disposition))
            == "supported-static"
        )


async def test_detail_missing_durable_support_downgrades_supported_history(
    supported_finding, monkeypatch
):
    from app.services.scan_reads import ScanReadService

    async def read(*args):
        return None

    monkeypatch.setattr(ScanReadService, "read_in_session", read)
    case = supported_finding
    detail = await FindingsQueryService(case.factory).get_finding_detail(
        case.ctx, case.repository_id, case.snapshot_id, case.finding_id
    )
    assert detail.disposition == "unresolved"
    assert detail.historical_disposition == "supported-static"
    assert detail.support_reason == "source_unavailable"


async def test_detail_does_not_promote_unresolved_history(authority, monkeypatch):
    from app.services.scan_reads import ScanReadService

    async def unsupported_read(*args):
        raise AssertionError("historical unresolved finding must not be promoted")

    monkeypatch.setattr(ScanReadService, "read_in_session", unsupported_read)
    detail = await FindingsQueryService(authority.factory).get_finding_detail(
        authority.ctx, authority.repository_id, authority.snapshot_id, authority.finding_id
    )
    assert detail.historical_disposition == "unresolved"
    assert detail.disposition == "unresolved"


async def test_detail_never_opens_uploaded_archive_in_api(supported_finding, monkeypatch):
    from app.services.scan_reads import ScanReadService

    case = supported_finding
    reads = []

    class ArchiveStore:
        async def open_verified(self, descriptor):
            reads.append(descriptor)
            yield b"uploaded archive must stay isolated"

    async def no_support(*args):
        return None

    monkeypatch.setattr(ScanReadService, "read_in_session", no_support)
    detail = await FindingsQueryService(case.factory).get_finding_detail(
        case.ctx, case.repository_id, case.snapshot_id, case.finding_id, ArchiveStore()
    )
    assert detail.code_excerpt is None
    assert reads == []


@pytest.mark.parametrize("effective", ["supported-static", "unresolved"])
async def test_list_filters_counts_and_summary_use_current_support(
    supported_finding, monkeypatch, effective
):
    from app.services.scan_reads import ScanReadService

    case = supported_finding

    async def read(*args):
        return SimpleNamespace(
            effective_decisions=(
                EffectiveVerificationV1(
                    finding_id=case.finding_id,
                    historical_disposition="supported-static",
                    effective_disposition=effective,
                    reason="current_support"
                    if effective == "supported-static"
                    else "source_unavailable",
                ),
            )
        )

    monkeypatch.setattr(ScanReadService, "read_in_session", read)
    service = FindingsQueryService(case.factory)
    page = await service.list_findings(
        case.ctx, case.repository_id, case.snapshot_id, case.run_id, disposition=effective, limit=1
    )
    assert page.total_count == 1
    assert len(page.items) == 1
    assert page.items[0].disposition == effective
    assert page.items[0].historical_disposition == "supported-static"
    other = "unresolved" if effective == "supported-static" else "supported-static"
    empty = await service.list_findings(
        case.ctx, case.repository_id, case.snapshot_id, case.run_id, disposition=other
    )
    assert empty.total_count == 0 and not empty.items
    summary = await service.get_summary(case.ctx, case.repository_id, case.snapshot_id, case.run_id)
    assert summary.by_disposition == {effective: 1}
    assert summary.by_historical_disposition == {"supported-static": 1}

"""Durable command replay and scoped keyset behavior on disposable SQLite."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.contracts.triage import TriageTransitionV1
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.models.scan_tables import finding_occurrences, finding_triage_events
from app.models.tables import source_files
from app.services.findings_query import FindingsQueryService
from app.services.triage import TriageService
from tests.phase4.unit.test_findings_authorization import authority as authority_fixture
from tests.phase4.unit.test_scan_status_api import status_client as status_client_fixture

authority = authority_fixture
status_client = status_client_fixture


@pytest_asyncio.fixture
async def commands_ready(authority):
    # Mirror the authority fixture's dialect adaptation without altering production metadata.
    metadata = sa.MetaData()
    for table in PHASE2_TABLES["api_commands"].metadata.tables.values():
        table.to_metadata(metadata)
    commands = metadata.tables["api_commands"]
    for constraint in commands.constraints:
        if isinstance(constraint, sa.CheckConstraint):
            constraint.ddl_if(dialect="postgresql")
    commands.c.response.type = sa.JSON()
    async with authority.factory() as session, session.begin():
        connection = await session.connection()
        await connection.run_sync(lambda sync: commands.create(sync))
    return authority


async def transition(case, service, key, state="confirmed", version=0):
    return await service.transition(
        case.ctx,
        case.repository_id,
        case.snapshot_id,
        case.finding_id,
        TriageTransitionV1(state=state, expected_version=version),
        request_key=key,
    )


async def test_exact_request_replay_returns_original_response_after_later_edit(commands_ready):
    case = commands_ready
    service = TriageService(case.factory)
    first = await transition(case, service, "first")
    await transition(case, service, "later", "unreviewed", 1)
    assert await transition(case, service, "first") == first
    async with case.factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(finding_triage_events)) == 2
        )
        assert (
            await session.scalar(
                sa.select(sa.func.count()).select_from(PHASE2_TABLES["api_commands"])
            )
            == 2
        )


async def test_changed_payload_conflicts_and_replay_rechecks_permission(commands_ready):
    case = commands_ready
    service = TriageService(case.factory)
    await transition(case, service, "first")
    with pytest.raises(DuplicateConflict):
        await transition(case, service, "first", "unreviewed", 1)
    async with case.factory() as session, session.begin():
        await session.execute(
            sa.update(PHASE2_TABLES["repository_grants"]).values(revoked_at=datetime.now(UTC))
        )
    with pytest.raises(ScopeViolation):
        await transition(case, service, "first")


async def test_noop_request_is_remembered_without_an_event(commands_ready):
    case = commands_ready
    service = TriageService(case.factory)
    first = await transition(case, service, "noop", "unreviewed", 0)
    await transition(case, service, "later")
    assert await transition(case, service, "noop", "unreviewed", 0) == first
    async with case.factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(finding_triage_events)) == 1
        )


@pytest_asyncio.fixture
async def findings_case(authority):
    async with authority.factory() as session, session.begin():
        await session.execute(sa.delete(finding_occurrences))
        file_id = await session.scalar(
            sa.select(source_files.c.id).where(source_files.c.snapshot_id == authority.snapshot_id)
        )
        for index, severity in enumerate(["low", "high", "low", "high", "high"], 1):
            await session.execute(
                finding_occurrences.insert().values(
                    org_id=authority.ctx.org_id,
                    snapshot_id=authority.snapshot_id,
                    run_id=authority.run_id,
                    id=UUID(int=(0xABCDEF << 104) + index),
                    file_id=file_id,
                    observation_key=str(index),
                    category="source_pattern",
                    rule_id="fixture.rule",
                    payload_json={"title": "Fixture", "severity": severity},
                    canonical_text="{}",
                    payload_digest="a" * 64,
                )
            )
    return authority


async def page(case, service, **params):
    return await service.list_findings(
        case.ctx, case.repository_id, case.snapshot_id, case.run_id, **params
    )


async def test_severity_filter_precedes_count_and_page_limit(findings_case):
    case = findings_case
    service = FindingsQueryService(case.factory)
    first = await page(case, service, severity="high", limit=2)
    assert [item.id.int - (0xABCDEF << 104) for item in first.items] == [2, 4]
    assert first.total_count == 3 and first.next_cursor
    second = await page(case, service, severity="high", limit=2, cursor=first.next_cursor)
    assert [item.id.int - (0xABCDEF << 104) for item in second.items] == [5]
    assert second.total_count == 3 and second.next_cursor is None


async def test_cursor_rejects_filter_changes_and_invalid_tokens(findings_case):
    case = findings_case
    service = FindingsQueryService(case.factory)
    first = await page(case, service, severity="high", limit=1)
    with pytest.raises(ValueError):
        await page(case, service, severity="low", cursor=first.next_cursor)
    with pytest.raises(ValueError):
        await page(case, service, cursor="invalid")


async def test_history_pages_use_timestamp_and_id_tiebreaker(authority):
    case = authority
    timestamp = datetime.now(UTC)
    async with case.factory() as session, session.begin():
        for index in range(1, 6):
            await session.execute(
                finding_triage_events.insert().values(
                    org_id=case.ctx.org_id,
                    snapshot_id=case.snapshot_id,
                    run_id=case.run_id,
                    finding_id=case.finding_id,
                    event_id=UUID(int=(0xABCDEF << 104) + index),
                    principal_id=case.ctx.principal_id,
                    state="confirmed",
                    reason="none",
                    created_at=timestamp,
                )
            )
    service = TriageService(case.factory)
    ids = []
    cursor = None
    while True:
        result = await service.get_history(
            case.ctx, case.repository_id, case.snapshot_id, case.finding_id, cursor=cursor, limit=2
        )
        assert len(result.events) <= 2
        ids.extend(event.event_id.int - (0xABCDEF << 104) for event in result.events)
        cursor = result.next_cursor
        if cursor is None:
            break
    assert ids == [5, 4, 3, 2, 1]


async def test_failed_command_record_rolls_back_audit_and_projection(commands_ready, monkeypatch):
    case = commands_ready

    async def fail_record(*args):
        raise RuntimeError("synthetic response persistence failure")

    monkeypatch.setattr("app.services.triage.record_command", fail_record)
    with pytest.raises(RuntimeError):
        await transition(case, TriageService(case.factory), "failure")
    async with case.factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(finding_triage_events)) == 0
        )
        assert (
            await session.scalar(
                sa.select(sa.func.count()).select_from(PHASE2_TABLES["api_commands"])
            )
            == 0
        )


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"severity": "urgent"},
        {"category": "all"},
        {"disposition": "verified"},
        {"triage_state": "ignored"},
    ],
)
async def test_invalid_finding_query_is_rejected(findings_case, params):
    with pytest.raises(ValueError):
        await page(findings_case, FindingsQueryService(findings_case.factory), **params)


@pytest.mark.parametrize("key", ["", "x" * 129, "é" * 65])
async def test_triage_request_key_has_utf8_byte_bound(commands_ready, key):
    with pytest.raises(ValueError):
        await transition(commands_ready, TriageService(commands_ready.factory), key)


async def test_history_rejects_invalid_cursor_and_limits(authority):
    service = TriageService(authority.factory)
    args = (authority.ctx, authority.repository_id, authority.snapshot_id, authority.finding_id)
    for kwargs in ({"cursor": "invalid"}, {"limit": 0}, {"limit": 101}):
        with pytest.raises(ValueError):
            await service.get_history(*args, **kwargs)


async def test_http_requires_request_key_and_replays_response(commands_ready, status_client):
    case = commands_ready
    path = (
        f"/api/v1/repositories/{case.repository_id}/snapshots/{case.snapshot_id}"
        f"/findings/{case.finding_id}/triage"
    )
    body = {"state": "confirmed", "expected_version": 0}
    assert (await status_client.post(path, json=body)).status_code == 422
    first = await status_client.post(path, json=body, headers={"idempotency-key": "http-command"})
    assert first.status_code == 200, first.text
    replay = await status_client.post(path, json=body, headers={"idempotency-key": "http-command"})
    assert replay.status_code == 200 and replay.json() == first.json()
    changed = await status_client.post(
        path,
        json={"state": "unreviewed", "expected_version": 1},
        headers={"idempotency-key": "http-command"},
    )
    assert changed.status_code == 409


async def test_http_rejects_invalid_pagination(authority, status_client):
    base = f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}"
    for path in (
        f"{base}/scans/{authority.run_id}/findings?severity=urgent",
        f"{base}/scans/{authority.run_id}/findings?cursor=invalid",
        f"{base}/findings/{authority.finding_id}/triage/history?limit=101",
        f"{base}/findings/{authority.finding_id}/triage/history?cursor=invalid",
    ):
        response = await status_client.get(path)
        assert response.status_code == 422, response.text


async def test_request_response_is_not_shared_across_principals(commands_ready):
    from uuid import uuid4

    from app.models.tables import memberships

    case = commands_ready
    service = TriageService(case.factory)
    await transition(case, service, "same-key")
    other = case.ctx.model_copy(update={"principal_id": uuid4()})
    async with case.factory() as session, session.begin():
        await session.execute(
            memberships.insert().values(
                org_id=other.org_id, principal_id=other.principal_id, role="member"
            )
        )
        await session.execute(
            PHASE2_TABLES["repository_grants"]
            .insert()
            .values(
                org_id=other.org_id,
                repository_id=case.repository_id,
                principal_id=other.principal_id,
                permission="write",
            )
        )
    with pytest.raises(DuplicateConflict):
        await service.transition(
            other,
            case.repository_id,
            case.snapshot_id,
            case.finding_id,
            TriageTransitionV1(state="confirmed", expected_version=0),
            request_key="same-key",
        )


async def test_current_support_filter_precedes_keyset_count_and_page(findings_case, monkeypatch):
    from types import SimpleNamespace

    from app.contracts.scan_publication import EffectiveVerificationV1
    from app.models.scan_tables import verification_decisions
    from app.services.scan_reads import ScanReadService

    case = findings_case
    ids = [UUID(int=(0xABCDEF << 104) + i) for i in range(1, 6)]
    async with case.factory() as session, session.begin():
        for identity in ids:
            await session.execute(
                verification_decisions.insert().values(
                    org_id=case.ctx.org_id,
                    snapshot_id=case.snapshot_id,
                    run_id=case.run_id,
                    finding_id=identity,
                    disposition="supported-static",
                    payload_json={},
                    canonical_text="{}",
                    payload_digest="a" * 64,
                )
            )

    async def read(*args):
        return SimpleNamespace(
            effective_decisions=tuple(
                EffectiveVerificationV1(
                    finding_id=identity,
                    historical_disposition="supported-static",
                    effective_disposition="unresolved" if i % 2 else "supported-static",
                    reason="source_unavailable" if i % 2 else "current_support",
                )
                for i, identity in enumerate(ids, 1)
            )
        )

    monkeypatch.setattr(ScanReadService, "read_in_session", read)
    service = FindingsQueryService(case.factory)
    first = await page(case, service, disposition="unresolved", limit=2)
    assert [f.id for f in first.items] == [ids[0], ids[2]]
    assert first.total_count == 3 and first.next_cursor
    last = await page(case, service, disposition="unresolved", limit=2, cursor=first.next_cursor)
    assert [f.id for f in last.items] == [ids[4]]
    assert last.total_count == 3 and last.next_cursor is None
    assert all(f.historical_disposition == "supported-static" for f in first.items + last.items)

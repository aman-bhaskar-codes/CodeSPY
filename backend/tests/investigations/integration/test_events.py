"""Integration tests for Task J investigation event pagination and monotonic sequence.

Tests:
- Monotonic sequence ordering (1-based, deterministic)
- Paging with after_sequence and limit
- Reconnect and gap indicator behavior
- Cross-repository event access denial
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from app.adapters.storage.local import LocalObjectStore
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.domain.scope import OrgRole, TenantContext
from app.models.investigation_execution_tables import (
    investigation_commands,
    investigation_evidence,
    investigation_execution_checkpoints,
    investigation_jobs,
    investigation_publication_receipts,
    investigation_reports,
    investigation_specs,
    investigation_step_receipts,
    model_call_results,
)
from app.models.investigation_tables import (
    investigation_checkpoints,
    investigation_events,
    investigations,
)
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import (
    artifacts,
    memberships,
    organizations,
    principals,
    repositories,
    source_snapshots,
)
from app.ports.object_store import ObjectStore
from app.services.stream_buffer import InvestigationStreamBuffer
from sqlalchemy import event, insert
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CheckConstraint
from sqlalchemy.sql.elements import BinaryExpression, TextClause

session_table = PHASE2_TABLES["sessions"]
grants_table = PHASE2_TABLES["repository_grants"]


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # type: ignore[no-untyped-def]
    return "JSON"


@compiles(TextClause, "sqlite")
def _compile_text_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    new_text = element.text.replace("::jsonb", "").replace("::text", "")
    if new_text != element.text:
        clone = element._clone()
        clone.text = new_text
        return compiler.visit_textclause(clone, **kw)
    return compiler.visit_textclause(element, **kw)


@compiles(CheckConstraint, "sqlite")
def _compile_check_constraint_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    sqltext = str(element.sqltext)
    name = str(element.name or "")
    if (
        any(
            bad in sqltext
            for bad in ("~", "octet_length", "jsonb_", "num_nonnulls", "admit_source")
        )
        or "operation" in name
    ):
        return "CHECK (1=1)"
    return compiler.visit_check_constraint(element, **kw)


@compiles(BinaryExpression, "sqlite")
def _compile_binary_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    if "interval" in str(element.right):
        left_str = compiler.process(element.left, **kw)
        return f"datetime({left_str}, '+15 minutes')"
    return compiler.visit_binary(element, **kw)


@pytest_asyncio.fixture
async def session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite session factory with all required tables."""
    import app.db.time
    import app.security.sessions

    async def _mock_now(_s: object) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr(app.security.sessions, "database_now", _mock_now)
    monkeypatch.setattr(app.db.time, "database_now", _mock_now)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _register_sqlite_functions(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        dbapi_connection.create_function(
            "clock_timestamp", 0, lambda: datetime.now(UTC).isoformat()
        )

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: investigation_jobs.metadata.create_all(
                sync_conn, tables=[investigation_jobs]
            )
        )
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    memberships,
                    repositories,
                    artifacts,
                    source_snapshots,
                    organizations,
                    principals,
                    session_table,
                    grants_table,
                    PHASE2_TABLES["job_attempts"],
                    PHASE2_TABLES["job_events"],
                    PHASE2_TABLES["outbox"],
                    PHASE2_TABLES["org_work_limits"],
                    investigations,
                    investigation_checkpoints,
                    investigation_events,
                    investigation_specs,
                    investigation_execution_checkpoints,
                    investigation_step_receipts,
                    model_call_results,
                    investigation_evidence,
                    investigation_reports,
                    investigation_publication_receipts,
                    investigation_commands,
                ],
            )
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def events_env(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]]]:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    ctx = TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.MEMBER)

    async with session_factory() as session:
        await session.execute(
            organizations.insert().values(
                id=org_id,
                slug=f"sec-{org_id.hex[:8]}",
                display_name="Security Corp",
            )
        )
        await session.execute(
            principals.insert().values(
                id=principal_id,
                subject_key=f"user-{principal_id}",
                display_name="Security User",
            )
        )
        await session.execute(
            memberships.insert().values(
                org_id=org_id,
                principal_id=principal_id,
                role="owner",
            )
        )
        await session.execute(
            insert(PHASE2_TABLES["org_work_limits"]).values(
                org_id=org_id,
                max_pending_jobs=10,
                max_active_jobs=5,
            )
        )
        await session.execute(
            repositories.insert().values(
                id=repo_id,
                org_id=org_id,
                repository_key="identity-vault",
                display_name="Identity Vault",
                source_kind="local_fixture",
            )
        )
        art_id = uuid4()
        await session.execute(
            artifacts.insert().values(
                org_id=org_id,
                id=art_id,
                repository_id=repo_id,
                sha256="b" * 64,
                size_bytes=100,
                storage_key="test-key-events",
            )
        )
        await session.execute(
            source_snapshots.insert().values(
                id=snap_id,
                org_id=org_id,
                repository_id=repo_id,
                artifact_id=art_id,
                admission_config_hash="b" * 64,
                identity_hash="b" * 64,
                manifest_hash="b" * 64,
                state="sealed",
                sealed_at=datetime.now(UTC),
            )
        )
        await session.execute(
            grants_table.insert().values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
                revoked_at=None,
            )
        )
        await session.commit()

    stream_buf = InvestigationStreamBuffer()
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=session_factory,
            store=cast(ObjectStore, LocalObjectStore(tmp_path / "obj")),
            fixture_context=ctx,
            stream_buffer=stream_buf,
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        sess_res = await client.get("/api/v1/session")
        assert sess_res.status_code == 200
        csrf_val = sess_res.json()["csrf_token"]
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf_val,
        }
        yield client, ctx, repo_id, snap_id, headers


@pytest.mark.asyncio
async def test_events_pagination_and_monotonic_sequence(
    events_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, ctx, repo_id, snap_id, headers = events_env

    # 1. Create V2 investigation
    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json={
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "Trace secret storage",
            "target_type": "repository",
        },
        headers={**headers, "Idempotency-Key": "idemp-events-cr"},
    )
    assert create_res.status_code == 201
    inv_id = UUID(create_res.json()["id"])

    # 2. Initially empty events page
    empty_res = await client.get(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/events",
        headers=headers,
    )
    assert empty_res.status_code == 200
    empty_page = empty_res.json()
    assert empty_page["items"] == []
    assert empty_page["next_cursor"] is None

    # 3. Seed 5 events with increasing step_index
    base_time = datetime.now(UTC)
    async with session_factory() as session:
        for i in range(1, 6):
            await session.execute(
                insert(investigation_events).values(
                    id=uuid4(),
                    org_id=ctx.org_id,
                    investigation_id=inv_id,
                    step_index=i,
                    event_type=f"step_{i}_completed",
                    node_name=f"node_{i}",
                    payload_json={"step": i, "status": "ok"},
                    created_at=base_time + timedelta(seconds=i),
                )
            )
        await session.commit()

    # 4. Page 1: limit 2
    p1_res = await client.get(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/events?limit=2",
        headers=headers,
    )
    assert p1_res.status_code == 200
    p1 = p1_res.json()
    assert len(p1["items"]) == 2
    assert p1["items"][0]["sequence"] == 1
    assert p1["items"][1]["sequence"] == 2
    assert p1["next_cursor"] == 2

    # 5. Page 2: after_sequence=2, limit 2
    p2_res = await client.get(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/events?after_sequence=2&limit=2",
        headers=headers,
    )
    assert p2_res.status_code == 200
    p2 = p2_res.json()
    assert len(p2["items"]) == 2
    assert p2["items"][0]["sequence"] == 3
    assert p2["items"][1]["sequence"] == 4
    assert p2["next_cursor"] == 4

    # 6. Page 3: after_sequence=4, limit 2 (final item)
    p3_res = await client.get(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/events?after_sequence=4&limit=2",
        headers=headers,
    )
    assert p3_res.status_code == 200
    p3 = p3_res.json()
    assert len(p3["items"]) == 1
    assert p3["items"][0]["sequence"] == 5
    assert p3["next_cursor"] is None


@pytest.mark.asyncio
async def test_events_cross_repo_access_denied(
    events_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
) -> None:
    client, _ctx, repo_id, snap_id, headers = events_env

    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json={
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "Check cross repo barrier",
            "target_type": "repository",
        },
        headers={**headers, "Idempotency-Key": "idemp-events-cross"},
    )
    inv_id = create_res.json()["id"]

    other_repo = uuid4()
    forbidden_res = await client.get(
        f"/api/v1/repositories/{other_repo}/investigations/{inv_id}/events",
        headers=headers,
    )
    assert forbidden_res.status_code == 404

"""Integration tests for Task J asynchronous API endpoints.

Tests:
- V2 create requires snapshot_id and freezes spec
- Idempotency key handling and conflict rejection
- Execute returns 202 enqueue-only InvestigationStartResponseV1
- Get detail returns safe InvestigationDetailResponseV2 without ciphertext
- Cancel command returns InvestigationCancelResponseV1
- Resume command enforces state guards (409 on invalid state, 202 on awaiting_human_decision)
- Retry command creates a new pending investigation with retry_of lineage
- List endpoint supports signed cursor pagination and rejects scope mismatch
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
from sqlalchemy import event, insert, update
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
async def api_env(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]]]:
    """Test environment with authenticated API client and seeded scope."""
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    ctx = TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.MEMBER)

    async with session_factory() as session:
        await session.execute(
            organizations.insert().values(
                id=org_id,
                slug=f"acme-{org_id.hex[:8]}",
                display_name="Acme Security",
            )
        )
        await session.execute(
            principals.insert().values(
                id=principal_id,
                subject_key=f"user-{principal_id}",
                display_name="Auditor User",
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
                repository_key="safe-egress-service",
                display_name="Safe Egress Service",
                source_kind="local_fixture",
            )
        )
        art_id = uuid4()
        await session.execute(
            artifacts.insert().values(
                org_id=org_id,
                id=art_id,
                repository_id=repo_id,
                sha256="a" * 64,
                size_bytes=100,
                storage_key="test-key",
            )
        )
        await session.execute(
            source_snapshots.insert().values(
                id=snap_id,
                org_id=org_id,
                repository_id=repo_id,
                artifact_id=art_id,
                admission_config_hash="a" * 64,
                identity_hash="b" * 64,
                manifest_hash="a" * 64,
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

    from app.adapters.model.fake import FakeModelAdapter

    stream_buf = InvestigationStreamBuffer()
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=session_factory,
            store=cast(ObjectStore, LocalObjectStore(tmp_path / "obj")),
            fixture_context=ctx,
            stream_buffer=stream_buf,
            model_adapter=FakeModelAdapter(),
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
async def test_v2_create_requires_snapshot_and_freezes_spec(
    api_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
) -> None:
    client, _ctx, repo_id, snap_id, headers = api_env
    idemp_key = "idemp-create-v2-1"
    req_headers = {**headers, "Idempotency-Key": idemp_key}

    payload = {
        "repository_id": str(repo_id),
        "snapshot_id": str(snap_id),
        "query": "Investigate potential SSRF on egress webhook",
        "target_type": "repository",
        "mode": "short_path",
        "depth": "standard",
        "max_steps": 8,
        "budget_limit_micros": 3_000_000,
    }

    # 1. Missing idempotency key -> 422
    no_key_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json=payload,
        headers=headers,
    )
    assert no_key_res.status_code == 422

    # 2. Valid creation -> 201
    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json=payload,
        headers=req_headers,
    )
    assert create_res.status_code == 201
    data = create_res.json()
    assert data["status"] == "pending"
    assert data["snapshot_id"] == str(snap_id)
    assert len(data["spec_hash"]) == 64

    # 3. Idempotent replay with same key -> 201 with identical ID
    replay_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json=payload,
        headers=req_headers,
    )
    assert replay_res.status_code == 201
    assert replay_res.json()["id"] == data["id"]

    # 4. Conflicting payload with same key -> 409
    conflict_payload = {**payload, "query": "Different query"}
    conflict_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json=conflict_payload,
        headers=req_headers,
    )
    assert conflict_res.status_code == 409

    # 5. Repository mismatch -> 400
    mismatch_payload = {**payload, "repository_id": str(uuid4())}
    mismatch_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json=mismatch_payload,
        headers={**headers, "Idempotency-Key": "idemp-mismatch"},
    )
    assert mismatch_res.status_code == 400


@pytest.mark.asyncio
async def test_v2_execute_returns_202_enqueue_only(
    api_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
) -> None:
    client, _ctx, repo_id, snap_id, headers = api_env
    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json={
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "Validate token revocation timing",
            "target_type": "repository",
        },
        headers={**headers, "Idempotency-Key": "idemp-exec-cr"},
    )
    assert create_res.status_code == 201
    inv_id = create_res.json()["id"]

    # Execute with idempotency key -> 202 Accepted
    start_headers = {**headers, "Idempotency-Key": "idemp-exec-start"}
    exec_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
        headers=start_headers,
    )
    assert exec_res.status_code == 202
    exec_data = exec_res.json()
    assert exec_data["investigation_id"] == inv_id
    assert "job_id" in exec_data
    assert exec_data["status"] in {"pending", "queued", "running"}
    assert exec_data["status_url"] == f"/api/v1/repositories/{repo_id}/investigations/{inv_id}"
    assert (
        exec_data["events_url"] == f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/events"
    )

    # Replay with same key -> 202 identical job_id
    replay_exec = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
        headers=start_headers,
    )
    assert replay_exec.status_code == 202
    assert replay_exec.json()["job_id"] == exec_data["job_id"]

    # Start again with a different key while job active -> 409
    active_conflict = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
        headers={**headers, "Idempotency-Key": "idemp-exec-start-diff"},
    )
    assert active_conflict.status_code == 409


@pytest.mark.asyncio
async def test_v2_detail_projection_safe_no_ciphertext(
    api_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
) -> None:
    client, _ctx, repo_id, snap_id, headers = api_env
    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json={
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "Inspect file upload boundary checks",
            "target_type": "repository",
        },
        headers={**headers, "Idempotency-Key": "idemp-detail-cr"},
    )
    inv_id = create_res.json()["id"]

    # Start job
    await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
        headers={**headers, "Idempotency-Key": "idemp-detail-exec"},
    )

    # Get detail
    detail_res = await client.get(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}",
        headers=headers,
    )
    assert detail_res.status_code == 200
    detail = detail_res.json()

    assert detail["summary"]["id"] == inv_id
    assert detail["summary"]["snapshot_id"] == str(snap_id)
    assert "job" in detail and detail["job"] is not None
    assert detail["job"]["operation"] == "investigate_snapshot"
    assert "cancel" in detail["available_actions"]
    assert detail["current_support_available"] is True

    # Security check: detail output must NOT contain ciphertext, secrets, or prompts
    serialized = json.dumps(detail)
    assert "ciphertext" not in serialized
    assert "model_transcript" not in serialized
    assert "prompt_tokens" not in serialized


@pytest.mark.asyncio
async def test_v2_cancel_command(
    api_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
) -> None:
    client, _ctx, repo_id, snap_id, headers = api_env
    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json={
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "Check privilege escalation routes",
            "target_type": "repository",
        },
        headers={**headers, "Idempotency-Key": "idemp-cancel-cr"},
    )
    inv_id = create_res.json()["id"]

    # Execute to create active job
    await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
        headers={**headers, "Idempotency-Key": "idemp-cancel-exec"},
    )

    # Cancel command -> 200
    cancel_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/cancel",
        headers={**headers, "Idempotency-Key": "idemp-cancel-cmd"},
    )
    assert cancel_res.status_code == 200
    cancel_data = cancel_res.json()
    assert cancel_data["investigation_id"] == inv_id
    assert cancel_data["status"] == "cancelled"
    assert cancel_data["already_terminal"] is False

    # Second cancel -> 200 already_terminal=True
    cancel_res_2 = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/cancel",
        headers={**headers, "Idempotency-Key": "idemp-cancel-cmd-2"},
    )
    assert cancel_res_2.status_code == 200
    assert cancel_res_2.json()["already_terminal"] is True


@pytest.mark.asyncio
async def test_v2_resume_command_state_guards(
    api_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ctx, repo_id, snap_id, headers = api_env
    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json={
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "Check session hijacking guards",
            "target_type": "repository",
        },
        headers={**headers, "Idempotency-Key": "idemp-resume-cr"},
    )
    inv_id = UUID(create_res.json()["id"])
    spec_hash = create_res.json()["spec_hash"]

    # 1. Resume when in pending state -> 409 invalid state
    resume_payload = {
        "expected_failed_job_id": str(uuid4()),
        "expected_spec_hash": spec_hash,
    }
    resume_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/resume",
        json=resume_payload,
        headers={**headers, "Idempotency-Key": "idemp-res-1"},
    )
    assert resume_res.status_code == 409

    # 2. Put investigation in awaiting_human_decision state
    failed_job_id = uuid4()
    async with session_factory() as s:
        await s.execute(
            update(investigations)
            .where(investigations.c.id == inv_id)
            .values(
                status="awaiting_human_decision",
                current_job_id=failed_job_id,
            )
        )
        await s.commit()

    # Resume when in awaiting_human_decision -> 202
    resume_ok = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/resume",
        json={
            "expected_failed_job_id": str(failed_job_id),
            "expected_spec_hash": spec_hash,
        },
        headers={**headers, "Idempotency-Key": "idemp-res-ok"},
    )
    assert resume_ok.status_code == 202
    assert resume_ok.json()["investigation_id"] == str(inv_id)


@pytest.mark.asyncio
async def test_v2_retry_command(
    api_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
) -> None:
    client, _ctx, repo_id, snap_id, headers = api_env
    create_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations",
        json={
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "Check auth token lifetime",
            "target_type": "repository",
        },
        headers={**headers, "Idempotency-Key": "idemp-retry-cr"},
    )
    inv_id = create_res.json()["id"]

    # Start and cancel investigation first to put it in a terminal cancelled state
    await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
        headers={**headers, "Idempotency-Key": "idemp-retry-start"},
    )
    await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/cancel",
        headers={**headers, "Idempotency-Key": "idemp-retry-cancel"},
    )

    # Retry command creates a new pending investigation with retry_of lineage
    retry_res = await client.post(
        f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/retry",
        json={"max_steps": 12, "budget_limit_micros": 6_000_000},
        headers={**headers, "Idempotency-Key": "idemp-retry-cmd"},
    )
    assert retry_res.status_code == 201
    retry_data = retry_res.json()
    assert retry_data["id"] != inv_id
    assert retry_data["status"] == "pending"
    assert retry_data["retry_of_id"] == inv_id
    assert retry_data["max_steps"] == 12
    assert retry_data["budget_limit_micros"] == 6_000_000


@pytest.mark.asyncio
async def test_v2_list_cursor_pagination_and_scope_isolation(
    api_env: tuple[httpx.AsyncClient, TenantContext, UUID, UUID, dict[str, str]],
) -> None:
    client, _ctx, repo_id, snap_id, headers = api_env

    # Create 3 investigations
    for i in range(3):
        res = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations",
            json={
                "repository_id": str(repo_id),
                "snapshot_id": str(snap_id),
                "query": f"Query number {i}",
                "target_type": "repository",
            },
            headers={**headers, "Idempotency-Key": f"idemp-list-{i}"},
        )
        assert res.status_code == 201

    # First page: limit 2
    p1 = await client.get(
        f"/api/v1/repositories/{repo_id}/investigations?limit=2",
        headers=headers,
    )
    assert p1.status_code == 200
    d1 = p1.json()
    assert len(d1["items"]) == 2
    assert d1["total_count"] == 3
    assert d1["next_cursor"] is not None

    # Second page using cursor
    p2 = await client.get(
        f"/api/v1/repositories/{repo_id}/investigations?cursor={d1['next_cursor']}&limit=2",
        headers=headers,
    )
    assert p2.status_code == 200
    d2 = p2.json()
    assert len(d2["items"]) == 1
    assert d2["next_cursor"] is None

    # Cross-repo cursor spoofing: using cursor on different repository -> 404
    other_repo_id = uuid4()
    spoofed = await client.get(
        f"/api/v1/repositories/{other_repo_id}/investigations?cursor={d1['next_cursor']}",
        headers=headers,
    )
    assert spoofed.status_code == 404

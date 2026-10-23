"""Integration tests for Phase 6 Investigation REST and SSE endpoints."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from app.adapters.model.fake import FakeModelAdapter
from app.adapters.storage.local import LocalObjectStore
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.investigation import (
    InvestigationStatus,
    InvestigationVerdict,
)
from app.contracts.model import ModelResponseV1, TokenUsageV1
from app.domain.scope import OrgRole, TenantContext
from app.models.budget_tables import embedding_budgets, price_schedules
from app.models.investigation_tables import (
    investigation_checkpoints,
    investigation_events,
    investigations,
)
from app.models.memory_tables import repo_memories
from app.models.model_gateway_tables import (
    model_capabilities,
    model_reservations,
    model_spend_ledger,
)
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import (
    memberships,
    organizations,
    principals,
    repositories,
    source_files,
    source_snapshots,
)
from app.ports.object_store import ObjectStore
from app.services.stream_buffer import InvestigationStreamBuffer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

grants = PHASE2_TABLES["repository_grants"]
session_table = PHASE2_TABLES["sessions"]


@pytest_asyncio.fixture
async def factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sqlalchemy import CheckConstraint

    source_files.c.path.type.collation = None  # type: ignore[attr-defined]
    for table in [source_snapshots, source_files, organizations, principals, session_table]:
        for c in list(table.constraints):
            if isinstance(c, CheckConstraint):
                table.constraints.discard(c)

    import app.db.time
    import app.security.sessions

    async def _mock_now(_s: object) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr(app.security.sessions, "database_now", _mock_now)
    monkeypatch.setattr(app.db.time, "database_now", _mock_now)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    organizations,
                    principals,
                    memberships,
                    repositories,
                    grants,
                    session_table,
                    source_snapshots,
                    source_files,
                    embedding_budgets,
                    price_schedules,
                    model_capabilities,
                    model_reservations,
                    model_spend_ledger,
                    investigations,
                    investigation_checkpoints,
                    investigation_events,
                    repo_memories,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_investigation_api_full_lifecycle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()

    ctx = TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.OWNER)

    # 1. Seed tenant and repository
    async with factory() as session, session.begin():
        await session.execute(
            organizations.insert().values(
                id=org_id,
                slug="sec-org",
                display_name="Security Org",
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
            repositories.insert().values(
                id=repo_id,
                org_id=org_id,
                repository_key="api-audit-repo",
                display_name="API Audit Repo",
                source_kind="local_fixture",
            )
        )
        await session.execute(
            grants.insert().values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
                revoked_at=None,
            )
        )

    # 2. Configure FakeModelAdapter with canned verdict
    canned_verdict = {
        "verdict": "benign",
        "confidence": 0.92,
        "summary": "Verified: endpoint does not expose credentials.",
        "claims": [],
    }
    mock_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content=f"```json\n{json.dumps(canned_verdict)}\n```",
        usage=TokenUsageV1(input_tokens=100, output_tokens=40),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="api-req-1",
    )
    fake_adapter = FakeModelAdapter(canned_responses=[mock_resp])
    stream_buf = InvestigationStreamBuffer()

    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=factory,
            store=cast(ObjectStore, LocalObjectStore(tmp_path / "obj")),
            fixture_context=ctx,
            stream_buffer=stream_buf,
            model_adapter=fake_adapter,
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Establish session
        sess_res = await client.get("/api/v1/session")
        assert sess_res.status_code == 200
        csrf_val = sess_res.json()["csrf_token"]
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf_val,
            "Idempotency-Key": "idemp-test-api-1",
        }

        # Step 1: Create investigation
        create_res = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations",
            json={
                "repository_id": str(repo_id),
                "query": "Investigate potential SSRF on webhook dispatcher",
                "mode": "short_path",
                "depth": "standard",
                "max_steps": 5,
                "budget_limit_micros": 2_000_000,
            },
            headers=headers,
        )
        assert create_res.status_code == 201, f"Response: {create_res.text}"
        inv_data = create_res.json()
        inv_id = inv_data["id"]
        assert inv_data["status"] == InvestigationStatus.PENDING.value
        assert inv_data["query"] == "Investigate potential SSRF on webhook dispatcher"

        # Step 2: List investigations
        list_res = await client.get(
            f"/api/v1/repositories/{repo_id}/investigations",
            headers=headers,
        )
        assert list_res.status_code == 200
        list_data = list_res.json()
        assert list_data["total_count"] >= 1
        assert list_data["items"][0]["id"] == inv_id

        # Step 3: Get initial detail
        detail_res = await client.get(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}",
            headers=headers,
        )
        assert detail_res.status_code == 200
        detail_data = detail_res.json()
        assert detail_data["summary"]["status"] == InvestigationStatus.PENDING.value

        # Step 4: Execute investigation
        exec_res = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
            headers=headers,
        )
        assert exec_res.status_code == 200, f"Exec error: {exec_res.text}"
        exec_data = exec_res.json()
        assert exec_data["summary"]["status"] == InvestigationStatus.COMPLETED.value
        assert exec_data["summary"]["verdict"] == InvestigationVerdict.BENIGN.value
        assert exec_data["summary"]["verdict_confidence"] == 0.92

        # Step 5: List audit events
        events_res = await client.get(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/events",
            headers=headers,
        )
        assert events_res.status_code == 200
        events = events_res.json()
        assert len(events) >= 2  # step_started, checkpoint_saved, verdict_reached
        event_types = [e["event_type"] for e in events]
        assert "verdict_reached" in event_types

        # Step 6: Submit human feedback with save_to_memory=True
        fb_res = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/feedback",
            json={
                "feedback_type": "agree",
                "notes": "Confirmed benign: webhook egress uses strict IP allowlist.",
                "save_to_memory": True,
                "memory_topic": "Webhook dispatcher SSRF disposition",
            },
            headers=headers,
        )
        assert fb_res.status_code == 200
        fb_data = fb_res.json()
        assert fb_data["status"] == "ok"
        assert fb_data["memory_entry_id"] is not None

        # Verify feedback event recorded
        events_after = (
            await client.get(
                f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/events",
                headers=headers,
            )
        ).json()
        assert any(e["event_type"] == "human_feedback" for e in events_after)

        # Step 7: Verify SSE stream replays events
        async with client.stream(
            "GET",
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/stream",
            headers={"Origin": "http://testserver"},
        ) as stream_resp:
            assert stream_resp.status_code == 200
            assert "text/event-stream" in stream_resp.headers["content-type"]
            first_chunk = await anext(stream_resp.aiter_text())
            assert "event: " in first_chunk
            assert "data: " in first_chunk

"""Unit tests for budget and recovery configuration validation and bounds."""

from pathlib import Path

import pytest
from app.contracts.accounting_recovery import AccountingRecoverySettingsV1
from pydantic import ValidationError


def test_recovery_settings_defaults() -> None:
    settings = AccountingRecoverySettingsV1()
    assert settings.enabled is False
    assert settings.interval_seconds == 15.0
    assert settings.batch_limit == 100
    assert settings.stale_age_seconds == 300


def test_recovery_settings_rejects_unbounded_duration_when_enabled() -> None:
    with pytest.raises(ValueError, match="bounded max_provider_call_duration_seconds required"):
        AccountingRecoverySettingsV1(enabled=True, max_provider_call_duration_seconds=None)


def test_recovery_settings_stale_age_bounds() -> None:
    # stale_age_seconds must be >= 60
    with pytest.raises(ValidationError):
        AccountingRecoverySettingsV1(stale_age_seconds=59)

    # batch_limit must be between 1 and 100
    with pytest.raises(ValidationError):
        AccountingRecoverySettingsV1(batch_limit=101)
    with pytest.raises(ValidationError):
        AccountingRecoverySettingsV1(batch_limit=0)


def test_recovery_settings_requires_stale_age_exceeds_call_duration_plus_margin() -> None:
    # call duration = 280, margin = 30 -> minimum stale age = 311
    with pytest.raises(ValueError, match=r"stale_age_seconds.*must exceed"):
        AccountingRecoverySettingsV1(
            enabled=True,
            max_provider_call_duration_seconds=280,
            recovery_margin_seconds=30,
            stale_age_seconds=300,
        )

    # Valid when stale_age exceeds call duration + margin
    valid = AccountingRecoverySettingsV1(
        enabled=True,
        max_provider_call_duration_seconds=200,
        recovery_margin_seconds=30,
        stale_age_seconds=300,
    )
    assert valid.enabled is True
    assert valid.stale_age_seconds == 300


@pytest.mark.asyncio
async def test_investigation_cannot_mint_budget_or_seed_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that an ordinary investigation fails with 409/402 when pricing or budget are unseeded,
    and succeeds once explicitly configured and funded by the operator.
    """
    import json
    from datetime import UTC, datetime
    from typing import cast
    from uuid import uuid4

    import app.db.time
    import app.security.sessions
    import httpx
    from app.adapters.model.fake import FakeModelAdapter
    from app.adapters.storage.local import LocalObjectStore
    from app.api.application import ApiSettings, ApplicationPorts, create_app
    from app.contracts.budget_operations import FundBudgetV1
    from app.contracts.model import ModelResponseV1, TokenUsageV1
    from app.domain.scope import OrgRole, TenantContext
    from app.models.budget_tables import budget_funding_receipts, embedding_budgets, price_schedules
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
    from app.services.budget_operations import BudgetOperationsService
    from app.services.model_budget import ModelBudgetService
    from app.services.stream_buffer import InvestigationStreamBuffer
    from sqlalchemy import CheckConstraint
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def _mock_now(_s: object) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr(app.security.sessions, "database_now", _mock_now)
    monkeypatch.setattr(app.db.time, "database_now", _mock_now)

    source_files.c.path.type.collation = None
    for table in [
        source_snapshots,
        source_files,
        organizations,
        principals,
        PHASE2_TABLES["sessions"],
        budget_funding_receipts,
    ]:
        for c in list(table.constraints):
            if isinstance(c, CheckConstraint):
                table.constraints.discard(c)

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
                    PHASE2_TABLES["repository_grants"],
                    PHASE2_TABLES["sessions"],
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
                    budget_funding_receipts,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    ctx = TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.OWNER)

    async with factory() as session, session.begin():
        await session.execute(
            organizations.insert().values(id=org_id, slug="o", display_name="Org")
        )
        await session.execute(
            principals.insert().values(
                id=principal_id, subject_key=f"u-{principal_id}", display_name="U"
            )
        )
        await session.execute(
            memberships.insert().values(org_id=org_id, principal_id=principal_id, role="owner")
        )
        await session.execute(
            repositories.insert().values(
                id=repo_id,
                org_id=org_id,
                repository_key="r",
                display_name="R",
                source_kind="local_fixture",
            )
        )
        await session.execute(
            PHASE2_TABLES["repository_grants"]
            .insert()
            .values(
                org_id=org_id, repository_id=repo_id, principal_id=principal_id, permission="write"
            )
        )

    canned_verdict = {
        "verdict": "benign",
        "confidence": 0.95,
        "summary": "Verified: benign code.",
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
        sess = await client.get("/api/v1/session")
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": sess.json()["csrf_token"],
            "Idempotency-Key": "test-key-1",
        }
        create_res = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations",
            json={
                "repository_id": str(repo_id),
                "query": "Check without price schedule",
                "mode": "short_path",
                "depth": "standard",
                "max_steps": 5,
                "budget_limit_micros": 100_000,
            },
            headers=headers,
        )
        assert create_res.status_code == 201
        inv_id = create_res.json()["id"]

        # 1. Unseeded execution must fail with 409 (price missing)
        exec_res1 = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
            headers=headers,
        )
        assert exec_res1.status_code == 409

        # 2. Operator publishes price schedule, but account is still missing -> fails with 402
        async with factory() as session, session.begin():
            budget_svc = ModelBudgetService(session)
            await budget_svc.ensure_default_generation_pricing()

        exec_res2 = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
            headers=headers,
        )
        assert exec_res2.status_code == 402

        # 3. Operator initializes account (0 balance) -> fails with 402 (insufficient spendable)
        ops_svc = BudgetOperationsService(factory)
        await ops_svc.init_budget(ctx, repo_id)

        exec_res3 = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
            headers=headers,
        )
        assert exec_res3.status_code == 402

        # 4. Operator explicitly funds account -> execution succeeds with 200
        await ops_svc.fund(
            ctx,
            repo_id,
            FundBudgetV1(
                request_key="fund-req-1",
                amount_micros=1_000_000,
                currency="USD",
                reason="initial_deposit",
                evidence_digest="e" * 64,
            ),
        )

        exec_res4 = await client.post(
            f"/api/v1/repositories/{repo_id}/investigations/{inv_id}/execute",
            headers=headers,
        )
        assert exec_res4.status_code == 200

    await engine.dispose()

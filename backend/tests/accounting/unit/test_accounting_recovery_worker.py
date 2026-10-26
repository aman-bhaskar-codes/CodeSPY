"""Unit tests for AccountingRecoveryWorker discover, bounded hold, and error containment."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.accounting_recovery import (
    AccountingRecoverySettingsV1,
)
from app.models.budget_tables import embedding_budgets
from app.models.model_gateway_tables import model_reservations, model_spend_ledger
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, repositories
from app.services.model_budget import ModelBudgetService
from app.workers.accounting_recovery import AccountingRecoveryWorker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

grants = PHASE2_TABLES["repository_grants"]


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        memberships,
        repositories,
        grants,
        embedding_budgets,
        model_reservations,
        model_spend_ledger,
    ]
    async with engine.begin() as conn:
        for table in tables:
            await conn.run_sync(table.create)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    yield fac
    await engine.dispose()


@pytest.mark.asyncio
async def test_accounting_recovery_disabled_by_default(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = AccountingRecoveryWorker(factory)
    summary = await worker.tick()
    assert summary.scanned == 0
    assert summary.held == 0
    assert summary.continuation_pending is False


@pytest.mark.asyncio
async def test_accounting_recovery_holds_stale_dispatch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    res_id = uuid4()

    stale_time = datetime.now(UTC) - timedelta(seconds=400)

    async with factory() as s:
        await s.execute(
            repositories.insert().values(
                org_id=org_id,
                id=repo_id,
                repository_key=str(repo_id),
                display_name="test-repo",
                source_kind="archive",
            )
        )
        await s.execute(
            embedding_budgets.insert().values(
                org_id=org_id,
                repository_id=repo_id,
                currency="USD",
                balance_micros=1000,
                reserved_micros=500,
                debt_micros=0,
                write_version=1,
            )
        )
        await s.execute(
            model_reservations.insert().values(
                org_id=org_id,
                reservation_id=res_id,
                repository_id=repo_id,
                principal_id=principal_id,
                investigation_id=uuid4(),
                logical_call_id=uuid4(),
                attempt_id=1,
                provider="fake",
                model_id="offline",
                charge_category="generation",
                estimated_input_tokens=100,
                estimated_output_tokens=50,
                reserved_micros=500,
                state="dispatched",
                idempotency_key="stale-key",
                created_at=stale_time,
                updated_at=stale_time,
            )
        )
        await s.commit()

    settings = AccountingRecoverySettingsV1(
        enabled=True,
        max_provider_call_duration_seconds=60,
        recovery_margin_seconds=30,
        stale_age_seconds=120,
    )
    worker = AccountingRecoveryWorker(factory, settings)
    summary = await worker.tick()

    assert summary.held == 1
    assert summary.scanned == 1
    assert summary.failed_scopes == 0

    async with factory() as s:
        res = (await s.execute(select(model_reservations))).mappings().one()
        assert res["state"] == "uncertain_hold"
        assert res["failure_reason"] == "abandoned_dispatch"


@pytest.mark.asyncio
async def test_accounting_recovery_scope_failure_does_not_halt_other_scopes(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    repo_broken = uuid4()
    repo_healthy = uuid4()
    principal_id = uuid4()

    stale_time = datetime.now(UTC) - timedelta(seconds=400)

    async with factory() as s:
        # Healthy repo has a budget row
        await s.execute(
            repositories.insert(),
            [
                {
                    "org_id": org_id,
                    "id": repo_broken,
                    "repository_key": str(repo_broken),
                    "display_name": "broken-repo",
                    "source_kind": "archive",
                },
                {
                    "org_id": org_id,
                    "id": repo_healthy,
                    "repository_key": str(repo_healthy),
                    "display_name": "healthy-repo",
                    "source_kind": "archive",
                },
            ],
        )
        # Only healthy has a budget row; broken lacks budget row so recover will fail
        await s.execute(
            embedding_budgets.insert().values(
                org_id=org_id,
                repository_id=repo_healthy,
                currency="USD",
                balance_micros=1000,
                reserved_micros=500,
                debt_micros=0,
                write_version=1,
            )
        )
        await s.execute(
            model_reservations.insert(),
            [
                {
                    "org_id": org_id,
                    "reservation_id": uuid4(),
                    "repository_id": repo_broken,
                    "principal_id": principal_id,
                    "investigation_id": uuid4(),
                    "logical_call_id": uuid4(),
                    "attempt_id": 1,
                    "provider": "fake",
                    "model_id": "offline",
                    "charge_category": "generation",
                    "estimated_input_tokens": 100,
                    "estimated_output_tokens": 50,
                    "reserved_micros": 500,
                    "state": "dispatched",
                    "idempotency_key": "broken-key",
                    "created_at": stale_time,
                    "updated_at": stale_time,
                },
                {
                    "org_id": org_id,
                    "reservation_id": uuid4(),
                    "repository_id": repo_healthy,
                    "principal_id": principal_id,
                    "investigation_id": uuid4(),
                    "logical_call_id": uuid4(),
                    "attempt_id": 1,
                    "provider": "fake",
                    "model_id": "offline",
                    "charge_category": "generation",
                    "estimated_input_tokens": 100,
                    "estimated_output_tokens": 50,
                    "reserved_micros": 500,
                    "state": "dispatched",
                    "idempotency_key": "healthy-key",
                    "created_at": stale_time,
                    "updated_at": stale_time,
                },
            ],
        )
        await s.commit()

    original_recover = ModelBudgetService.recover_dispatch_intents

    async def fake_recover(
        self: ModelBudgetService,
        org_id: UUID,
        repository_id: UUID,
        *,
        minimum_age_seconds: int = 300,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        if repository_id == repo_broken:
            raise RuntimeError("Database connection glitch for scope")
        return await original_recover(
            self,
            org_id,
            repository_id,
            minimum_age_seconds=minimum_age_seconds,
            limit=limit,
        )

    monkeypatch.setattr(ModelBudgetService, "recover_dispatch_intents", fake_recover)

    settings = AccountingRecoverySettingsV1(
        enabled=True,
        max_provider_call_duration_seconds=60,
        recovery_margin_seconds=30,
        stale_age_seconds=120,
    )
    worker = AccountingRecoveryWorker(factory, settings)
    summary = await worker.tick()

    assert summary.held == 1
    assert summary.scanned == 1
    assert summary.failed_scopes == 1

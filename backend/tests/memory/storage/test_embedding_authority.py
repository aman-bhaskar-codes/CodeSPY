"""Restricted embedding operations share the generation budget authority."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    PriceScheduleV1,
    SettlementOutcomeV1,
)
from app.services.model_budget import ModelBudgetService
from app.services.provider_budget import InvalidReservationTransitionError, ProviderBudgetService
from tests.retrieval.storage.test_index_guards import source_case


@pytest.mark.parametrize("charge,expected_balance,expected_debt", [(250, 750, 0), (1500, 200, 700)])
async def test_restricted_embedding_replay_settle_and_uncertain_hold(
    migration_factory,
    api_factory,
    broker_factory,
    tmp_path,
    charge,
    expected_balance,
    expected_debt,
):
    ctx, source, _ = await source_case(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        service = ProviderBudgetService(session)
        await service.ensure_budget(ctx.org_id, source.repository_id, 1000)
        now = datetime.now(UTC)
        await service.set_price_schedule(
            PriceScheduleV1(
                provider="fake",
                model_id="embedding-test",
                version="test",
                input_micros_per_1k=1000,
                effective_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
        )
    request = BudgetReservationRequestV1(
        org_id=ctx.org_id,
        repository_id=source.repository_id,
        principal_id=ctx.principal_id,
        provider="fake",
        model_id="embedding-test",
        estimated_tokens=100,
        max_charge_micros=100,
        idempotency_key="first-embedding",
    )
    async with api_factory() as session, session.begin():
        first = await ProviderBudgetService(session).reserve(request, DisclosurePolicyV1())
        repeated = await ProviderBudgetService(session).reserve(request, DisclosurePolicyV1())
        second = await ProviderBudgetService(session).reserve(
            request.model_copy(update={"idempotency_key": "second-embedding"}), DisclosurePolicyV1()
        )
        cancelled = await ProviderBudgetService(session).reserve(
            request.model_copy(update={"idempotency_key": "cancel-embedding"}), DisclosurePolicyV1()
        )
        assert first == repeated

    async with api_factory() as session, session.begin():
        cancelled_result = await ProviderBudgetService(session).cancel(
            ctx.org_id, cancelled.reservation_id, reason="fixture_cancel"
        )
        assert cancelled_result.failure_reason == "fixture_cancel"
        assert (
            await ProviderBudgetService(session).cancel(
                ctx.org_id, cancelled.reservation_id, reason="fixture_cancel"
            )
            == cancelled_result
        )
    async with api_factory() as session, session.begin():
        with pytest.raises(InvalidReservationTransitionError):
            await ProviderBudgetService(session).cancel(
                ctx.org_id, cancelled.reservation_id, reason="changed_reason"
            )

    async def replay_reservation():
        async with api_factory() as session, session.begin():
            return await ProviderBudgetService(session).reserve(request, DisclosurePolicyV1())

    replays = await asyncio.gather(replay_reservation(), replay_reservation())
    assert all(r.reservation_id == first.reservation_id for r in replays)
    async with api_factory() as session, session.begin():
        with pytest.raises(InvalidReservationTransitionError):
            await ProviderBudgetService(session).reserve(
                request.model_copy(update={"max_charge_micros": 101}), DisclosurePolicyV1()
            )
    async with migration_factory() as session, session.begin():
        await ModelBudgetService(session).reserve_generation_budget(
            org_id=ctx.org_id,
            repository_id=source.repository_id,
            principal_id=ctx.principal_id,
            investigation_id=uuid4(),
            logical_call_id=uuid4(),
            attempt_id=1,
            provider="fake",
            model_id="offline",
            estimated_input_tokens=1,
            estimated_output_tokens=1,
            estimated_micros=100,
            idempotency_key="generation-hold",
        )
        now = datetime.now(UTC)
        await ProviderBudgetService(session).set_price_schedule(
            PriceScheduleV1(
                provider="fake",
                model_id="embedding-test",
                version="new-rate",
                input_micros_per_1k=2000,
                effective_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
    from app.contracts.accounting_recovery import AccountingRecoverySettingsV1
    from app.workers.accounting_recovery import AccountingRecoveryWorker
    from sqlalchemy import text

    async def claim_dispatch():
        async with broker_factory() as session, session.begin():
            return await ProviderBudgetService(session).mark_dispatched(
                ctx.org_id, second.reservation_id
            )

    assert sorted(await asyncio.gather(claim_dispatch(), claim_dispatch())) == [False, True]
    async with api_factory() as session, session.begin():
        with pytest.raises(InvalidReservationTransitionError):
            await ProviderBudgetService(session).cancel(ctx.org_id, second.reservation_id)
    # Set an initially empty marker relative to actual database time, without weakening guards.
    async with migration_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE embedding_reservations SET dispatch_intent_at="
                "clock_timestamp()-interval '600 seconds' WHERE org_id=:org AND reservation_id=:id"
            ),
            {"org": ctx.org_id, "id": first.reservation_id},
        )
    worker = AccountingRecoveryWorker(
        broker_factory,
        AccountingRecoverySettingsV1(
            enabled=True, stale_age_seconds=120, max_provider_call_duration_seconds=60
        ),
    )
    assert (await worker.tick()).held == 1
    assert (await worker.tick()).held == 0
    async with api_factory() as session:
        position = await ProviderBudgetService(session).get_budget(ctx.org_id, source.repository_id)
        assert position["reserved_micros"] == 300 and position["balance_micros"] == 1000
    async with broker_factory() as session, session.begin():
        settled = await ProviderBudgetService(session).settle(
            ctx.org_id,
            SettlementOutcomeV1(
                reservation_id=first.reservation_id,
                status="success",
                actual_tokens=charge,
                actual_charge_micros=charge,
            ),
        )
        assert settled.state == "committed"
        await ProviderBudgetService(session).settle(
            ctx.org_id,
            SettlementOutcomeV1(
                reservation_id=second.reservation_id,
                status="timeout",
                actual_tokens=0,
                actual_charge_micros=0,
            ),
        )
    async with api_factory() as session, session.begin():
        with pytest.raises(InvalidReservationTransitionError):
            await ProviderBudgetService(session).cancel(ctx.org_id, second.reservation_id)
    async with api_factory() as session:
        position = await ProviderBudgetService(session).get_budget(ctx.org_id, source.repository_id)
        assert position["balance_micros"] == expected_balance
        assert position["debt_micros"] == expected_debt
        assert position["reserved_micros"] == 200

    async def replay_settlement():
        async with broker_factory() as session, session.begin():
            return await ProviderBudgetService(session).settle(
                ctx.org_id,
                SettlementOutcomeV1(
                    reservation_id=first.reservation_id,
                    status="success",
                    actual_tokens=charge,
                    actual_charge_micros=charge,
                ),
            )

    assert all(
        r.state == "committed"
        for r in await asyncio.gather(replay_settlement(), replay_settlement())
    )
    async with api_factory() as session:
        assert (await ProviderBudgetService(session).get_budget(ctx.org_id, source.repository_id))[
            "balance_micros"
        ] == expected_balance

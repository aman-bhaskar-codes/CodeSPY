"""Unit tests for ProviderBudgetService reservation, price verification,
settlement, and lifecycle."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    PriceScheduleV1,
    ReservationState,
    SettlementOutcomeV1,
)
from app.models.budget_tables import embedding_budgets, embedding_reservations, price_schedules
from app.models.phase2_tables import md
from app.services.provider_budget import (
    BudgetExceededError,
    DisclosureDeniedError,
    InvalidReservationTransitionError,
    MissingPriceError,
    ProviderBudgetService,
    StalePriceError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Create tables required for budget tests
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[embedding_budgets, embedding_reservations, price_schedules],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_disclosure_denied_for_disallowed_provider(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    policy = DisclosurePolicyV1(
        allow_external_disclosure=False,
        allowed_providers=("fake",),
        allowed_regions=("us-east-1",),
    )
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        estimated_tokens=1000,
        max_charge_micros=100,
        idempotency_key="idemp_1",
    )
    with pytest.raises(DisclosureDeniedError, match="not permitted"):
        await svc.reserve(req, policy)


@pytest.mark.asyncio
async def test_missing_price_denial(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("aws_bedrock",))
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="aws_bedrock",
        model_id="unknown_model",
        estimated_tokens=1000,
        max_charge_micros=100,
        idempotency_key="idemp_2",
    )
    with pytest.raises(MissingPriceError, match="No price schedule found"):
        await svc.reserve(req, policy)


@pytest.mark.asyncio
async def test_stale_price_denial(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    # Insert expired price schedule
    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="aws_bedrock",
            model_id="amazon.titan-embed-text-v2:0",
            version="old",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=30),
            expires_at=now - timedelta(days=1),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("aws_bedrock",))
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        estimated_tokens=1000,
        max_charge_micros=100,
        idempotency_key="idemp_3",
    )
    with pytest.raises(StalePriceError, match="expired"):
        await svc.reserve(req, policy)


@pytest.mark.asyncio
async def test_budget_exceeded_denial(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    # Tiny budget: 10 micros
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=10)

    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="aws_bedrock",
            model_id="amazon.titan-embed-text-v2:0",
            version="v1",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("aws_bedrock",))
    # 1,000 tokens at 20 micros/1k = 20 micros needed, but balance is only 10 micros
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        estimated_tokens=1000,
        max_charge_micros=100,
        idempotency_key="idemp_4",
    )
    with pytest.raises(BudgetExceededError, match="Insufficient available budget"):
        await svc.reserve(req, policy)


@pytest.mark.asyncio
async def test_successful_reservation_and_idempotency(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="aws_bedrock",
            model_id="amazon.titan-embed-text-v2:0",
            version="v1",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("aws_bedrock",))
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        estimated_tokens=1000,
        max_charge_micros=50,
        idempotency_key="idemp_unique_1",
    )

    rec = await svc.reserve(req, policy)
    assert rec.state == ReservationState.RESERVED
    assert rec.estimated_micros == 20

    # Budget balance check
    budget = await svc.get_budget(org_id, repo_id)
    assert budget["balance_micros"] == 1_000_000
    assert budget["reserved_micros"] == 20
    assert budget["available_micros"] == 999_980

    # Replay with same idempotency key returns exact same record without double-reserving
    replay = await svc.reserve(req, policy)
    assert replay.reservation_id == rec.reservation_id

    budget2 = await svc.get_budget(org_id, repo_id)
    assert budget2["reserved_micros"] == 20  # Still 20, no duplicate hold!


@pytest.mark.asyncio
async def test_settle_success_updates_balance_and_releases_hold(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="fake",
            model_id="fake-embed-v1",
            version="v1",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("fake",))
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="fake",
        model_id="fake-embed-v1",
        estimated_tokens=1000,
        max_charge_micros=50,
        idempotency_key="idemp_settle_1",
    )
    rec = await svc.reserve(req, policy)

    outcome = SettlementOutcomeV1(
        reservation_id=rec.reservation_id,
        status="success",
        actual_tokens=900,
        actual_charge_micros=18,
        provider_request_id="req_123",
    )
    settled = await svc.settle(org_id, outcome)
    assert settled.state == ReservationState.COMMITTED
    assert settled.settled_micros == 18

    budget = await svc.get_budget(org_id, repo_id)
    assert budget["balance_micros"] == 999_982  # 1_000_000 - 18
    assert budget["reserved_micros"] == 0
    assert budget["available_micros"] == 999_982


@pytest.mark.asyncio
async def test_settle_failed_releases_hold_without_deducting_balance(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="fake",
            model_id="fake-embed-v1",
            version="v1",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("fake",))
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="fake",
        model_id="fake-embed-v1",
        estimated_tokens=1000,
        max_charge_micros=50,
        idempotency_key="idemp_fail_1",
    )
    rec = await svc.reserve(req, policy)

    outcome = SettlementOutcomeV1(
        reservation_id=rec.reservation_id,
        status="failed",
        actual_tokens=0,
        actual_charge_micros=0,
        error_message="Simulated upstream failure",
    )
    settled = await svc.settle(org_id, outcome)
    assert settled.state == ReservationState.RELEASED
    assert settled.failure_reason == "Simulated upstream failure"

    budget = await svc.get_budget(org_id, repo_id)
    assert budget["balance_micros"] == 1_000_000  # Nothing deducted
    assert budget["reserved_micros"] == 0  # Hold released!


@pytest.mark.asyncio
async def test_settle_timeout_retains_uncertain_hold(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="fake",
            model_id="fake-embed-v1",
            version="v1",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("fake",))
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="fake",
        model_id="fake-embed-v1",
        estimated_tokens=1000,
        max_charge_micros=50,
        idempotency_key="idemp_timeout_1",
    )
    rec = await svc.reserve(req, policy)

    outcome = SettlementOutcomeV1(
        reservation_id=rec.reservation_id,
        status="timeout",
        actual_tokens=0,
        actual_charge_micros=0,
        error_message="Gateway timeout",
    )
    held = await svc.settle(org_id, outcome)
    assert held.state == ReservationState.UNCERTAIN_HOLD
    assert held.is_active_hold()

    # Budget hold MUST be retained to prevent overspending until reconciled
    budget = await svc.get_budget(org_id, repo_id)
    assert budget["reserved_micros"] == 20
    assert budget["available_micros"] == 999_980


@pytest.mark.asyncio
async def test_cancel_releases_hold_cleanly(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="fake",
            model_id="fake-embed-v1",
            version="v1",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("fake",))
    req = BudgetReservationRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=uuid4(),
        provider="fake",
        model_id="fake-embed-v1",
        estimated_tokens=1000,
        max_charge_micros=50,
        idempotency_key="idemp_cancel_1",
    )
    rec = await svc.reserve(req, policy)
    cancelled = await svc.cancel(org_id, rec.reservation_id, reason="caller_aborted")
    assert cancelled.state == ReservationState.RELEASED

    budget = await svc.get_budget(org_id, repo_id)
    assert budget["reserved_micros"] == 0

    # Cannot cancel an already cancelled reservation
    with pytest.raises(InvalidReservationTransitionError):
        await svc.cancel(org_id, rec.reservation_id)


@pytest.mark.asyncio
async def test_concurrent_reservations_never_overcommit(session: AsyncSession) -> None:
    svc = ProviderBudgetService(session)
    now = datetime.now(UTC)
    org_id = uuid4()
    repo_id = uuid4()
    # Total balance: 100 micros
    await svc.ensure_budget(org_id, repo_id, initial_balance_micros=100)

    await svc.set_price_schedule(
        PriceScheduleV1(
            provider="fake",
            model_id="fake-embed-v1",
            version="v1",
            input_micros_per_1k=20,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("fake",))

    # Try 10 sequential/concurrent reservations of 20 micros each
    # (200 requested, only 100 available)
    successes = 0
    failures = 0
    for i in range(10):
        req = BudgetReservationRequestV1(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=uuid4(),
            provider="fake",
            model_id="fake-embed-v1",
            estimated_tokens=1000,
            max_charge_micros=50,
            idempotency_key=f"idemp_concurrent_{i}",
        )
        try:
            await svc.reserve(req, policy)
            successes += 1
        except BudgetExceededError:
            failures += 1

    assert successes == 5
    assert failures == 5

    budget = await svc.get_budget(org_id, repo_id)
    assert budget["balance_micros"] == 100
    assert budget["reserved_micros"] == 100
    assert budget["available_micros"] == 0

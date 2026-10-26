"""Immutable price versions and reservation-aligned output limits; simulated models."""

from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.model.fake import FakeModelAdapter
from app.contracts.model import GenerationPriceScheduleV1
from app.models.model_gateway_tables import model_reservations
from app.services.model_budget import ModelBudgetService, ModelInvalidStateTransitionError
from app.services.model_router import ModelCapabilityMismatchError
from sqlalchemy import func, select

from tests.phase6.unit import test_model_gateway as fixtures

budget_fixture = fixtures.session


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("output_micros_per_1k", 9000), ("expires_at", None)])
async def test_existing_price_version_cannot_change(budget_fixture, field, value):
    now = datetime.now(UTC)
    price = GenerationPriceScheduleV1(
        provider="fake",
        model_id="offline",
        version="fixed-one",
        input_micros_per_1k=500,
        output_micros_per_1k=1000,
        effective_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
    )
    budget = ModelBudgetService(budget_fixture)
    await budget.set_generation_price_schedule(price)
    await budget.set_generation_price_schedule(price)
    changed = price.model_copy(update={field: now + timedelta(days=2) if value is None else value})
    with pytest.raises(ModelInvalidStateTransitionError):
        await budget.set_generation_price_schedule(changed)
    actual = await budget.get_generation_price_schedule("fake", "offline", "fixed-one")
    assert actual.output_micros_per_1k == 1000
    assert actual.expires_at.replace(tzinfo=UTC) == price.expires_at


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [(None, 500), (3000, 3000)])
async def test_reservation_matches_dispatched_output_limit(budget_fixture, requested, expected):
    adapter = FakeModelAdapter(default_content='{"status":"ok"}')
    gateway, _budget, args = await fixtures.configured_gateway(budget_fixture, adapter)
    if requested is not None:
        args["max_tokens"] = requested
    await gateway.execute_call(**args)
    row = (await budget_fixture.execute(select(model_reservations))).mappings().one()
    assert row["estimated_output_tokens"] == expected
    assert adapter.call_history[0].max_tokens == expected


@pytest.mark.asyncio
async def test_model_output_capacity_rejects_before_reservation(budget_fixture):
    adapter = FakeModelAdapter(default_content='{"status":"ok"}')
    gateway, _budget, args = await fixtures.configured_gateway(budget_fixture, adapter)
    with pytest.raises(ModelCapabilityMismatchError):
        await gateway.execute_call(**args, max_tokens=9000)
    assert adapter.call_history == []
    assert await budget_fixture.scalar(select(func.count()).select_from(model_reservations)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", [0, -1, 32769])
async def test_invalid_output_limit_has_no_accounting_effect(budget_fixture, maximum):
    gateway, _budget, args = await fixtures.configured_gateway(budget_fixture, FakeModelAdapter())
    with pytest.raises(ValueError):
        await gateway.execute_call(**args, max_tokens=maximum)
    assert await budget_fixture.scalar(select(func.count()).select_from(model_reservations)) == 0

"""Configured profiles reserve the exact selected price through existing accounting."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.contracts.model import GenerationPriceScheduleV1, ModelRoutingRequirementV1
from app.models.model_gateway_tables import model_reservations
from app.services.model_budget import ModelBudgetService
from app.services.model_registry import ModelRegistryError
from app.services.model_router import ModelRouter
from sqlalchemy import func, select

from tests.model_connections.unit.test_registry import policy, profile, registry
from tests.phase6.unit import test_model_gateway as fixtures

budget_fixture = fixtures.session


async def configured(session):
    p = profile()
    budget = ModelBudgetService(session)
    now = datetime.now(UTC)
    schedule = GenerationPriceScheduleV1(
        provider=p.provider.value,
        model_id=p.model_id,
        version=p.price_version,
        input_micros_per_1k=500,
        output_micros_per_1k=1000,
        effective_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
    )
    await budget.set_generation_price_schedule(schedule)
    org, repo = uuid4(), uuid4()
    await budget.ensure_budget(org, repo, 10000)
    args = {
        "org_id": org,
        "repository_id": repo,
        "principal_id": uuid4(),
        "investigation_id": uuid4(),
        "logical_call_id": uuid4(),
        "attempt_id": 1,
        "requirement": ModelRoutingRequirementV1(
            estimated_context_tokens=100, estimated_output_tokens=50
        ),
        "disclosure_policy": policy(),
        "idempotency_key": "configured-call",
        "request_digest": "a" * 64,
        "requested_alias": "openai_fast",
    }
    return p, budget, schedule, args


@pytest.mark.asyncio
async def test_registry_selection_connects_to_existing_reservation(budget_fixture):
    p, budget, _schedule, args = await configured(budget_fixture)
    result = await ModelRouter(budget, registry=registry([p])).route_and_reserve(**args)
    assert result.provider == "openai"
    assert result.model_id == p.model_id
    assert result.price_schedule_version == p.price_version
    assert result.model_alias == p.alias
    assert len(result.profile_digest) == 64
    row = (await budget_fixture.execute(select(model_reservations))).mappings().one()
    assert row["reserved_micros"] == 100
    assert row["request_digest"] != args["request_digest"]


@pytest.mark.asyncio
async def test_unavailable_exact_alias_never_reserves_fake(budget_fixture):
    p, budget, _schedule, args = await configured(budget_fixture)
    with pytest.raises(ModelRegistryError):
        await ModelRouter(budget, registry=registry([p])).route_and_reserve(
            **{**args, "requested_alias": "claude_fast"}
        )
    assert await budget_fixture.scalar(select(func.count()).select_from(model_reservations)) == 0


@pytest.mark.asyncio
async def test_profile_does_not_silently_use_newer_price(budget_fixture):
    p, budget, schedule, args = await configured(budget_fixture)
    await budget.set_generation_price_schedule(
        schedule.model_copy(
            update={
                "version": "new-price",
                "input_micros_per_1k": 999,
                "effective_at": datetime.now(UTC) - timedelta(hours=1),
            }
        )
    )
    result = await ModelRouter(budget, registry=registry([p])).route_and_reserve(**args)
    assert result.estimated_micros == 100
    assert result.price_schedule_version == p.price_version

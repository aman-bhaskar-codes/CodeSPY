"""Persisted request digests and captured pricing with simulated inference."""

import json

import pytest
from app.adapters.model.fake import FakeModelAdapter
from app.contracts.model import GenerationPriceScheduleV1
from app.domain.canonical_json import semantic_digest
from app.models.model_gateway_tables import model_reservations
from app.services.model_budget import ModelBudgetService, ModelInvalidStateTransitionError
from sqlalchemy import select
from tests.phase6.unit import test_model_gateway as fixtures
from tests.phase6.unit.test_model_budget_transitions import reserved

budget_fixture = fixtures.session


@pytest.mark.asyncio
async def test_gateway_stores_digest_and_price_without_prompt(budget_fixture):
    gateway, _service, args = await fixtures.configured_gateway(
        budget_fixture, FakeModelAdapter(default_content='{"status":"ok"}')
    )
    await gateway.execute_call(**args)
    row = dict((await budget_fixture.execute(select(model_reservations))).mappings().one())
    assert len(row["request_digest"]) == 64
    snapshot = GenerationPriceScheduleV1.model_validate(row["price_snapshot"])
    assert snapshot.provider == row["provider"]
    assert snapshot.model_id == row["model_id"]
    assert "source-canary-78219" not in json.dumps(row, default=str)
    captured = await ModelBudgetService(budget_fixture).get_reserved_price(
        row["org_id"], row["reservation_id"]
    )
    assert captured == snapshot


@pytest.mark.asyncio
async def test_changed_prompt_digest_cannot_reuse_reserved_identity(budget_fixture):
    service, args, _legacy_id = await reserved(budget_fixture)
    await service.ensure_default_generation_pricing()
    price = await service.get_generation_price_schedule("fake", "codespy-fake-investigator-v1")
    bound = {
        **args,
        "idempotency_key": "bound-call",
        "model_id": price.model_id,
        "request_digest": semantic_digest({"message": "first"}),
        "price_snapshot": price,
    }
    identity = await service.reserve_generation_budget(**bound)
    assert await service.reserve_generation_budget(**bound) == identity
    with pytest.raises(ModelInvalidStateTransitionError):
        await service.reserve_generation_budget(
            **{**bound, "request_digest": semantic_digest({"message": "changed"})}
        )
    with pytest.raises(ModelInvalidStateTransitionError):
        await service.get_reserved_price(args["org_id"], _legacy_id)

"""Model accounting transition invariants with an explicitly SQLite fixture."""

from uuid import uuid4

import pytest
from app.models.model_gateway_tables import model_reservations, model_spend_ledger
from app.services.model_budget import ModelBudgetService, ModelInvalidStateTransitionError
from sqlalchemy import func, select
from tests.phase6.unit import test_model_gateway as fixtures

budget_fixture = fixtures.session


async def reserved(database):
    service = ModelBudgetService(database)
    args = {
        "org_id": uuid4(),
        "repository_id": uuid4(),
        "principal_id": uuid4(),
        "investigation_id": uuid4(),
        "logical_call_id": uuid4(),
        "attempt_id": 1,
        "provider": "fake",
        "model_id": "offline",
        "estimated_input_tokens": 10,
        "estimated_output_tokens": 20,
        "estimated_micros": 100,
        "idempotency_key": "call-one",
    }
    await service.ensure_budget(args["org_id"], args["repository_id"], 1000)
    identity = await service.reserve_generation_budget(**args)
    return service, args, identity


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_id", None),
        ("repository_id", None),
        ("logical_call_id", None),
        ("estimated_micros", 99),
        ("estimated_output_tokens", 21),
        ("provider", "other"),
    ],
)
async def test_reservation_replay_rejects_changed_binding(budget_fixture, field, value):
    service, args, identity = await reserved(budget_fixture)
    assert await service.reserve_generation_budget(**args) == identity
    with pytest.raises(ModelInvalidStateTransitionError):
        await service.reserve_generation_budget(
            **{**args, field: uuid4() if value is None else value}
        )
    assert (await service.get_budget(args["org_id"], args["repository_id"]))[
        "reserved_micros"
    ] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["dispatch_intent_committed", "dispatched", "uncertain_hold"])
async def test_release_cannot_return_potentially_spent_capacity(budget_fixture, state):
    service, args, identity = await reserved(budget_fixture)
    await service.commit_dispatch_intent(args["org_id"], identity)
    if state != "dispatch_intent_committed":
        await service.mark_dispatched(args["org_id"], identity)
    if state == "uncertain_hold":
        await service.hold_uncertain(args["org_id"], identity)
    with pytest.raises(ModelInvalidStateTransitionError):
        await service.release_reservation(args["org_id"], identity)
    assert (await service.get_budget(args["org_id"], args["repository_id"]))[
        "reserved_micros"
    ] == 100


@pytest.mark.asyncio
async def test_uncertain_hold_is_idempotent_and_cannot_reopen_settlement(budget_fixture):
    service, args, identity = await reserved(budget_fixture)
    await service.commit_dispatch_intent(args["org_id"], identity)
    await service.mark_dispatched(args["org_id"], identity)
    await service.hold_uncertain(args["org_id"], identity)
    await service.hold_uncertain(args["org_id"], identity)
    assert await budget_fixture.scalar(select(func.count()).select_from(model_spend_ledger)) == 1
    await service.settle_reservation(args["org_id"], identity, 10, 5, 0, 50)
    await service.hold_uncertain(args["org_id"], identity)
    assert await budget_fixture.scalar(select(model_reservations.c.state)) == "settled"
    assert await budget_fixture.scalar(select(func.count()).select_from(model_spend_ledger)) == 2


@pytest.mark.asyncio
async def test_undispatched_reservation_cannot_be_settled_or_marked_uncertain(budget_fixture):
    service, args, identity = await reserved(budget_fixture)
    with pytest.raises(ModelInvalidStateTransitionError):
        await service.settle_reservation(args["org_id"], identity, 1, 1, 0, 10)
    with pytest.raises(ModelInvalidStateTransitionError):
        await service.hold_uncertain(args["org_id"], identity)
    await service.release_reservation(args["org_id"], identity)
    await service.release_reservation(args["org_id"], identity)
    assert (await service.get_budget(args["org_id"], args["repository_id"]))["reserved_micros"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("actual_micros", 51),
        ("actual_input_tokens", 11),
        ("actual_output_tokens", 6),
        ("provider_request_id", "other"),
    ],
)
async def test_settlement_replay_rejects_changed_usage(budget_fixture, field, value):
    service, args, identity = await reserved(budget_fixture)
    await service.commit_dispatch_intent(args["org_id"], identity)
    await service.mark_dispatched(args["org_id"], identity)
    usage = {
        "org_id": args["org_id"],
        "reservation_id": identity,
        "actual_input_tokens": 10,
        "actual_output_tokens": 5,
        "actual_thinking_tokens": 0,
        "actual_micros": 50,
        "provider_request_id": "original",
    }
    await service.settle_reservation(**usage)
    await service.settle_reservation(**usage)
    with pytest.raises(ModelInvalidStateTransitionError):
        await service.settle_reservation(**{**usage, field: value})
    assert await budget_fixture.scalar(select(func.count()).select_from(model_spend_ledger)) == 1

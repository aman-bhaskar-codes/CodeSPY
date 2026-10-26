"""Exact overrun accounting and protected shared holds with simulated providers."""

from uuid import uuid4

import pytest
from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    SettlementOutcomeV1,
)
from app.models.budget_tables import embedding_reservations
from app.models.model_gateway_tables import model_spend_ledger
from app.services.model_budget import ModelBudgetExceededError
from app.services.provider_budget import BudgetExceededError, ProviderBudgetService
from sqlalchemy import select

from tests.phase6.unit import test_model_gateway as fixtures
from tests.phase6.unit.test_model_budget_transitions import reserved

budget_fixture = fixtures.session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "charge,expected_balance,expected_debt", [(50, 950, 0), (950, 100, 50), (1500, 100, 600)]
)
async def test_generation_overrun_preserves_other_hold(
    budget_fixture, charge, expected_balance, expected_debt
):
    service, args, identity = await reserved(budget_fixture)
    await service.reserve_generation_budget(
        **{**args, "idempotency_key": "other-call", "logical_call_id": uuid4()}
    )
    await service.commit_dispatch_intent(args["org_id"], identity)
    await service.mark_dispatched(args["org_id"], identity)
    await service.settle_reservation(args["org_id"], identity, 10, 5, 0, charge)
    budget = await service.get_budget(args["org_id"], args["repository_id"])
    assert budget["balance_micros"] == expected_balance
    assert budget["reserved_micros"] == 100
    assert budget["debt_micros"] == expected_debt
    assert await budget_fixture.scalar(select(model_spend_ledger.c.charged_micros)) == charge


@pytest.mark.asyncio
@pytest.mark.parametrize("settler", ["generation", "embedding"])
async def test_shared_debt_blocks_new_work_and_release_pays_debt(budget_fixture, settler):
    connection = await budget_fixture.connection()
    await connection.run_sync(lambda sync: embedding_reservations.create(sync, checkfirst=True))
    service, args, identity = await reserved(budget_fixture)
    await service.ensure_default_generation_pricing()
    embedding = ProviderBudgetService(budget_fixture)
    request = BudgetReservationRequestV1(
        org_id=args["org_id"],
        repository_id=args["repository_id"],
        principal_id=args["principal_id"],
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        estimated_tokens=200,
        max_charge_micros=100,
        idempotency_key="embedding-call",
    )
    policy = DisclosurePolicyV1()
    other = await embedding.reserve(request, policy)
    if settler == "generation":
        await service.commit_dispatch_intent(args["org_id"], identity)
        await service.mark_dispatched(args["org_id"], identity)
        await service.settle_reservation(args["org_id"], identity, 10, 5, 0, 1500)
    else:
        await embedding.settle(
            args["org_id"],
            SettlementOutcomeV1(
                reservation_id=other.reservation_id,
                status="success",
                actual_tokens=3000,
                actual_charge_micros=1500,
            ),
        )
    with pytest.raises(ModelBudgetExceededError):
        await service.reserve_generation_budget(
            **{
                **args,
                "idempotency_key": "new-generation",
                "logical_call_id": uuid4(),
                "estimated_micros": 0,
            }
        )
    with pytest.raises(BudgetExceededError):
        await embedding.reserve(
            request.model_copy(update={"idempotency_key": "new-embedding"}), policy
        )
    if settler == "generation":
        await embedding.cancel(args["org_id"], other.reservation_id)
    else:
        await service.release_reservation(args["org_id"], identity)
    budget = await service.get_budget(args["org_id"], args["repository_id"])
    assert budget["balance_micros"] == budget["reserved_micros"] == budget["available_micros"] == 0
    assert budget["debt_micros"] == 500

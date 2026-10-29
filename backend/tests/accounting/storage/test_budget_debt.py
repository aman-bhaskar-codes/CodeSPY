"""Real PostgreSQL exact-charge and outstanding-hold conservation."""

import asyncio
from uuid import uuid4

import pytest
from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    SettlementOutcomeV1,
)
from app.models.model_gateway_tables import model_spend_ledger
from app.services.model_budget import ModelBudgetService
from app.services.provider_budget import ProviderBudgetService
from scripts import phase2_db as harness
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from tests.accounting.storage.test_model_transitions import setup


async def calls(factory, tmp_path):
    args = await setup(factory, tmp_path)
    async with factory() as session, session.begin():
        service = ModelBudgetService(session)
        first = await service.reserve_generation_budget(**args)
        second = await service.reserve_generation_budget(
            **{**args, "idempotency_key": "second", "logical_call_id": uuid4()}
        )
        for identity in (first, second):
            await service.commit_dispatch_intent(args["org_id"], identity)
            await service.mark_dispatched(args["org_id"], identity)
    return args, first, second


async def test_concurrent_overruns_record_exact_total(migration_factory, tmp_path):
    args, first, second = await calls(migration_factory, tmp_path)
    ready = asyncio.Barrier(2)

    async def settle(identity, charge):
        async with migration_factory() as session, session.begin():
            await ready.wait()
            await ModelBudgetService(session).settle_reservation(
                args["org_id"], identity, 10, 5, 0, charge
            )

    async with asyncio.timeout(10):
        await asyncio.gather(settle(first, 1500), settle(second, 700))
    async with migration_factory() as session:
        budget = await ModelBudgetService(session).get_budget(args["org_id"], args["repository_id"])
        assert budget["balance_micros"] == budget["reserved_micros"] == 0
        assert budget["debt_micros"] == 1200
        assert await session.scalar(select(func.sum(model_spend_ledger.c.charged_micros))) == 2200


@pytest.mark.schema_revision("0027_model_reconciliation")
async def test_debt_blocks_lossy_downgrade_and_invalid_values(
    migration_factory, db_values, tmp_path
):
    args, first, _second = await calls(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        await ModelBudgetService(session).settle_reservation(args["org_id"], first, 10, 5, 0, 1500)
    with pytest.raises(DBAPIError, match="cannot downgrade outstanding budget debt"):
        harness.migrate(
            db_values,
            db_values["CODESPY_TEST_DATABASE"],
            "0024_model_request_identity",
            downgrade=True,
        )
    async with migration_factory() as session:
        with pytest.raises(DBAPIError, match="ck_budget_debt_nonneg"):
            await session.execute(text("UPDATE embedding_budgets SET debt_micros=-1"))
        await session.rollback()
        budget = await ModelBudgetService(session).get_budget(args["org_id"], args["repository_id"])
        assert budget["debt_micros"] == 600 and budget["reserved_micros"] == 100


async def test_embedding_overrun_and_generation_release_share_debt(migration_factory, tmp_path):
    args = await setup(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        service = ModelBudgetService(session)
        model = await service.reserve_generation_budget(**args)
        await service.ensure_default_generation_pricing()
        embedding = ProviderBudgetService(session)
        reservation = await embedding.reserve(
            BudgetReservationRequestV1(
                org_id=args["org_id"],
                repository_id=args["repository_id"],
                principal_id=args["principal_id"],
                provider="fake",
                model_id="codespy-fake-investigator-v1",
                estimated_tokens=200,
                max_charge_micros=100,
                idempotency_key="embedding-overrun",
            ),
            DisclosurePolicyV1(),
        )
        await embedding.settle(
            args["org_id"],
            SettlementOutcomeV1(
                reservation_id=reservation.reservation_id,
                status="success",
                actual_tokens=3000,
                actual_charge_micros=1500,
            ),
        )
        await service.release_reservation(args["org_id"], model)
    async with migration_factory() as session:
        budget = await ModelBudgetService(session).get_budget(args["org_id"], args["repository_id"])
        assert budget["debt_micros"] == 500
        assert budget["balance_micros"] == budget["reserved_micros"] == 0


@pytest.mark.schema_revision("0027_model_reconciliation")
async def test_zero_debt_roundtrip_preserves_cash_and_holds(migration_factory, db_values, tmp_path):
    args = await setup(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        await ModelBudgetService(session).reserve_generation_budget(**args)
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0024_model_request_identity", downgrade=True)
    harness.migrate(db_values, name, "0025_budget_debt")
    async with migration_factory() as session:
        budget = await ModelBudgetService(session).get_budget(args["org_id"], args["repository_id"])
        assert budget["balance_micros"] == 1000 and budget["reserved_micros"] == 100
        assert budget["debt_micros"] == 0

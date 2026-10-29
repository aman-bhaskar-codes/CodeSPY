"""Measured cache usage survives settlement and blocks lossy downgrade."""

import pytest
from app.models.model_gateway_tables import model_spend_ledger
from app.services.model_budget import ModelBudgetService
from scripts import phase2_db as harness
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.accounting.storage.test_budget_debt import calls


@pytest.mark.schema_revision("0027_model_reconciliation")
async def test_cache_usage_persists_and_guards_downgrade(migration_factory, db_values, tmp_path):
    args, first, second = await calls(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        service = ModelBudgetService(session)
        await service.hold_uncertain(args["org_id"], second)
        await service.settle_reservation(
            args["org_id"],
            first,
            10,
            5,
            2,
            50,
            actual_cache_read_tokens=30,
            actual_cache_write_tokens=40,
        )
    async with migration_factory() as session:
        rows = (await session.execute(select(model_spend_ledger))).mappings().all()
        settled = next(row for row in rows if not row["is_uncertain_hold"])
        uncertain = next(row for row in rows if row["is_uncertain_hold"])
        assert (settled["cache_read_tokens"], settled["cache_write_tokens"]) == (30, 40)
        assert uncertain["cache_read_tokens"] is uncertain["cache_write_tokens"] is None
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE model_spend_ledger SET cache_read_tokens=-1"))
        await session.rollback()
    with pytest.raises(DBAPIError, match="cannot downgrade measured cache usage"):
        harness.migrate(
            db_values, db_values["CODESPY_TEST_DATABASE"], "0025_budget_debt", downgrade=True
        )

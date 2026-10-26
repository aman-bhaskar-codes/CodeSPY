"""Financial routine parity with the existing integer accounting reference."""

import pytest
from app.repositories.accounting_repository import AccountingRepository
from app.services.model_budget import ModelBudgetService, ModelInvalidStateTransitionError

from tests.accounting.storage.test_budget_debt import calls


async def test_authority_settlement_deducts_exact_known_charge(migration_factory, tmp_path):
    args, first, _ = await calls(migration_factory, tmp_path)
    async with migration_factory() as s, s.begin():
        await AccountingRepository(s).settle_generation(
            args["org_id"], first, 10, 5, 0, 50, "receipt"
        )
    async with migration_factory() as s:
        position = await ModelBudgetService(s).get_budget(args["org_id"], args["repository_id"])
        assert position["balance_micros"] == 950
        assert position["reserved_micros"] == 100
    async with migration_factory() as s, s.begin():
        with pytest.raises(ModelInvalidStateTransitionError):
            await AccountingRepository(s).settle_generation(
                args["org_id"], first, 10, 5, 0, 99, "receipt"
            )

"""PostgreSQL embedding requests must use constrained accounting operations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.contracts.provider_budget import BudgetReservationRequestV1, DisclosurePolicyV1
from app.repositories.accounting_repository import AccountingRepository
from app.services.provider_budget import ProviderBudgetService


async def test_postgres_reservation_delegates_without_direct_financial_sql(monkeypatch):
    class DelegatedError(Exception):
        pass

    async def reserve(self, request):
        raise DelegatedError

    monkeypatch.setattr(AccountingRepository, "reserve_embedding_request", reserve, raising=False)
    session = SimpleNamespace(
        bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=AsyncMock(side_effect=AssertionError("direct financial SQL")),
    )
    request = BudgetReservationRequestV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        principal_id=uuid4(),
        provider="fake",
        model_id="fixture",
        estimated_tokens=10,
        max_charge_micros=100,
        idempotency_key="request",
    )
    with pytest.raises(DelegatedError):
        await ProviderBudgetService(session).reserve(request, DisclosurePolicyV1())

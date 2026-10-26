"""Unit qualification of positive funding contracts and bounded validation."""

import pytest
from app.contracts.budget_operations import FundBudgetV1
from pydantic import ValidationError


def test_fund_budget_contract_valid() -> None:
    req = FundBudgetV1(
        request_key="deposit-001",
        amount_micros=500_000,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="e" * 64,
    )
    assert req.amount_micros == 500_000
    assert req.currency == "USD"
    assert req.reason == "initial_deposit"


def test_fund_budget_contract_rejects_non_positive() -> None:
    for bad in (0, -1, -100_000):
        with pytest.raises(ValidationError):
            FundBudgetV1(
                request_key="key-1",
                amount_micros=bad,
                currency="USD",
                reason="initial_deposit",
                evidence_digest="a" * 64,
            )


def test_fund_budget_contract_rejects_currency_mismatch() -> None:
    for bad_curr in ("EUR", "GBP", "usd", "BTC"):
        with pytest.raises(ValidationError):
            FundBudgetV1(
                request_key="key-1",
                amount_micros=1000,
                currency=bad_curr,  # type: ignore[arg-type]
                reason="initial_deposit",
                evidence_digest="a" * 64,
            )


def test_fund_budget_contract_rejects_invalid_digest() -> None:
    for bad_digest in ("a" * 63, "a" * 65, "A" * 64, "invalid-hex"):
        with pytest.raises(ValidationError):
            FundBudgetV1(
                request_key="key-1",
                amount_micros=1000,
                currency="USD",
                reason="initial_deposit",
                evidence_digest=bad_digest,
            )


def test_fund_budget_contract_rejects_invalid_reason() -> None:
    with pytest.raises(ValidationError):
        FundBudgetV1(
            request_key="key-1",
            amount_micros=1000,
            currency="USD",
            reason="arbitrary_mint",  # type: ignore[arg-type]
            evidence_digest="a" * 64,
        )

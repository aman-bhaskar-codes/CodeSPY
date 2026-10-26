"""Unit qualification of positive funding arithmetic, debt repayment, and hold protection."""

import pytest
from app.domain.budget_position import BudgetPosition


def test_fund_validation_bounds() -> None:
    pos = BudgetPosition(balance_micros=100, reserved_micros=50, debt_micros=0)
    for bad in (0, -1, -500, True, False, 1.5, "100"):
        with pytest.raises(ValueError):
            pos.fund(bad)  # type: ignore[arg-type]


def test_fund_overflow_guard() -> None:
    pos = BudgetPosition(
        balance_micros=9223372036854775800,
        reserved_micros=0,
        debt_micros=0,
    )
    with pytest.raises(ValueError, match="overflow"):
        pos.fund(10)


def test_fund_oracle_case() -> None:
    # Oracle from specification:
    # balance=100, reserved=100, debt=300; fund 200 -> (100,100,100);
    # fund another 150 -> (150,100,0), available=50
    pos0 = BudgetPosition(balance_micros=100, reserved_micros=100, debt_micros=300)

    pos1, credited1, repaid1 = pos0.fund(200)
    assert (pos1.balance_micros, pos1.reserved_micros, pos1.debt_micros) == (100, 100, 100)
    assert credited1 == 0
    assert repaid1 == 200
    assert pos1.available_micros == 0

    pos2, credited2, repaid2 = pos1.fund(150)
    assert (pos2.balance_micros, pos2.reserved_micros, pos2.debt_micros) == (150, 100, 0)
    assert credited2 == 50
    assert repaid2 == 100
    assert pos2.available_micros == 50


def test_net_balance_minus_debt_increases_by_exact_funding() -> None:
    pos = BudgetPosition(balance_micros=500, reserved_micros=500, debt_micros=1200)
    net_before = pos.balance_micros - pos.debt_micros
    amount = 800

    next_pos, credited, repaid = pos.fund(amount)
    net_after = next_pos.balance_micros - next_pos.debt_micros

    assert net_after - net_before == amount
    assert credited + repaid == amount
    assert next_pos.reserved_micros == pos.reserved_micros  # hold untouched

"""Domain contracts for explicit owner budget funding and immutable receipts."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.domain.scope import Contract

FundingReason = Literal["initial_deposit", "manual_adjustment", "grant_replenishment"]


class FundBudgetV1(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    amount_micros: int = Field(gt=0, le=9223372036854775807)
    currency: Literal["USD"] = "USD"
    reason: FundingReason
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class BudgetFundingReceiptV1(Contract):
    audit_id: UUID
    credited_micros: int = Field(ge=0)
    debt_repaid_micros: int = Field(ge=0)
    resulting_balance_micros: int = Field(ge=0)
    resulting_reserved_micros: int = Field(ge=0)
    resulting_debt_micros: int = Field(ge=0)

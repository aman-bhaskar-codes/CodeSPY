"""Integer accounting that protects outstanding holds and records unfunded charges."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BudgetPosition:
    balance_micros: int
    reserved_micros: int
    debt_micros: int

    def __post_init__(self) -> None:
        for value in (self.balance_micros, self.reserved_micros, self.debt_micros):
            if type(value) is not int or not 0 <= value <= 9223372036854775807:
                raise ValueError("Invalid budget monetary bounds")
        if self.reserved_micros > self.balance_micros:
            raise ValueError("Budget holds exceed funded balance")
        if self.debt_micros and self.balance_micros != self.reserved_micros:
            raise ValueError("Available cash must first offset budget debt")

    @classmethod
    def from_row(cls, row: Mapping[Any, Any]) -> "BudgetPosition":
        return cls(row["balance_micros"], row["reserved_micros"], row["debt_micros"])

    @property
    def available_micros(self) -> int:
        return 0 if self.debt_micros else self.balance_micros - self.reserved_micros

    def complete(self, released_hold: int, actual_charge: int = 0) -> "BudgetPosition":
        if (
            type(released_hold) is not int
            or not 0 <= released_hold <= self.reserved_micros
            or type(actual_charge) is not int
            or not 0 <= actual_charge <= 9223372036854775807
        ):
            raise ValueError("Invalid budget settlement bounds")
        remaining = self.reserved_micros - released_hold
        owed = self.debt_micros + actual_charge
        payment = min(owed, self.balance_micros - remaining)
        return BudgetPosition(self.balance_micros - payment, remaining, owed - payment)

    def update_values(self) -> dict[str, int]:
        return {
            "balance_micros": self.balance_micros,
            "reserved_micros": self.reserved_micros,
            "debt_micros": self.debt_micros,
        }

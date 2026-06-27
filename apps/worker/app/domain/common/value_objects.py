from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Money:
    amount: int
    currency: str = "KRW"

    def __post_init__(self) -> None:
        if self.currency != "KRW":
            raise ValueError("Only KRW is supported in the MVP")
        if self.amount < 0:
            raise ValueError("Money amount must be non-negative")


@dataclass(frozen=True, slots=True)
class Symbol:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.isdigit() or len(self.value) != 6:
            raise ValueError("Korean stock symbol must be a 6-digit string")


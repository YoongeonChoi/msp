from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: int
    avg_price_krw: int
    current_price_krw: int
    sector: str

    @property
    def market_value_krw(self) -> int:
        return self.quantity * self.current_price_krw


from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriceBar:
    symbol: str
    close_krw: int
    volume: int
    turnover_krw: int


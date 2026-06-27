from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuarterlyFundamentals:
    symbol: str
    per: float | None
    pbr: float | None
    roe: float | None
    operating_margin: float | None
    debt_ratio: float | None


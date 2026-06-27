from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Exposure:
    symbol_pct: float
    sector_pct: float


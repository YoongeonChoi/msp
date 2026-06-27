from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class MarketCalendarDay:
    market: str
    day: date
    is_open: bool


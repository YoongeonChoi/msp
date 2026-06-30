from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class MarketCalendarDay:
    market: str
    day: date
    is_open: bool


@dataclass(frozen=True, slots=True)
class MarketSectorEvidence:
    symbol: str
    market: str
    sector: str
    industry: str | None
    source: str
    as_of: datetime

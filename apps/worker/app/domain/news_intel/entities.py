from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class NewsClassification:
    symbol: str
    relevance_score: float
    sentiment: Literal["positive", "neutral", "negative", "unknown"]
    event_type: str
    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    summary_short: str
    trading_relevance: float
    confidence: float


@dataclass(frozen=True, slots=True)
class NewsEvent:
    symbol: str
    title: str
    source: str
    published_at: datetime
    classification: NewsClassification


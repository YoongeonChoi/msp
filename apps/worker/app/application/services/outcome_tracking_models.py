from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import assert_never

from app.domain.common.json import JsonObject


class OutcomeStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETE = "complete"
    SKIPPED = "skipped"


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class DecisionForOutcome:
    decision_id: str
    symbol: str
    side: TradeSide | None
    decided_at: datetime | None
    price_at_decision: float | None
    feature_snapshot: JsonObject


@dataclass(frozen=True, slots=True)
class PricePoint:
    symbol: str
    trade_date: date
    close_price: float


@dataclass(frozen=True, slots=True)
class PaperOrderForOutcome:
    order_id: str
    decision_id: str
    status: str
    price: float | None
    quantity: float | None
    amount_krw: float | None


@dataclass(frozen=True, slots=True)
class OutcomeTrackingSummary:
    processed_count: int
    upserted_count: int
    skipped_count: int
    partial_count: int
    complete_count: int


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    decision_id: str
    order_id: str | None
    symbol: str
    price_at_decision: float | None
    return_1d: float | None
    return_5d: float | None
    return_20d: float | None
    max_drawdown_20d: float | None
    hit_target: bool | None
    hit_stop: bool | None
    realized_pnl_krw: int | None
    status: OutcomeStatus
    reason: str | None
    calculated_at: datetime

    def to_row(self) -> JsonObject:
        return {
            "decision_id": self.decision_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "horizon_days": 20,
            "price_at_decision": self.price_at_decision,
            "return_1d": self.return_1d,
            "return_5d": self.return_5d,
            "return_20d": self.return_20d,
            "return_pct": self.return_20d,
            "max_drawdown_20d": self.max_drawdown_20d,
            "hit_target": self.hit_target,
            "hit_stop": self.hit_stop,
            "realized_pnl_krw": self.realized_pnl_krw,
            "pnl_krw": self.realized_pnl_krw,
            "outcome_status": self.status.value,
            "reason": self.reason,
            "calculated_at": self.calculated_at.isoformat(),
            "updated_at": self.calculated_at.isoformat(),
        }


def signed_return(side: TradeSide, entry_price: float, exit_price: float) -> float:
    match side:
        case TradeSide.BUY:
            return (exit_price - entry_price) / entry_price
        case TradeSide.SELL:
            return (entry_price - exit_price) / entry_price
        case unreachable:
            assert_never(unreachable)

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

TradingMode = Literal["paper", "live"]
OrderAction = Literal["hold", "buy", "sell"]
OrderStatus = Literal[
    "proposed",
    "paper",
    "blocked",
    "sent",
    "filled",
    "rejected",
    "failed",
    "unknown_requires_manual_check",
]


@dataclass(frozen=True, slots=True)
class BotSettings:
    enabled: bool = False
    mode: TradingMode = "paper"
    live_order_allowed: bool = False
    max_order_amount_krw: int = 100_000
    max_daily_loss_pct: float = 0.02
    max_daily_order_count: int = 10
    max_position_pct: float = 0.10
    max_sector_pct: float = 0.30
    loop_interval_sec: int = 30
    quote_freshness_sec: int = 60


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price_krw: int
    as_of: datetime
    source: str = "mock"


@dataclass(frozen=True, slots=True)
class AccountState:
    synced: bool
    cash_krw: int
    equity_krw: int
    daily_loss_pct: float
    daily_order_count: int
    synced_at: datetime


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    action: OrderAction
    final_score: float
    confidence: float
    order_amount_krw: int
    sector: str
    reason_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    id: UUID
    cycle_id: UUID
    signal: Signal
    strategy_version_id: UUID
    created_at: datetime
    feature_snapshot: dict[str, Any]
    risk_snapshot: dict[str, Any]

    @classmethod
    def create(
        cls,
        cycle_id: UUID,
        signal: Signal,
        strategy_version_id: UUID,
        created_at: datetime,
        feature_snapshot: dict[str, Any],
        risk_snapshot: dict[str, Any],
    ) -> DecisionSnapshot:
        return cls(
            id=uuid4(),
            cycle_id=cycle_id,
            signal=signal,
            strategy_version_id=strategy_version_id,
            created_at=created_at,
            feature_snapshot=feature_snapshot,
            risk_snapshot=risk_snapshot,
        )


@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    decision_id: UUID
    symbol: str
    action: Literal["buy", "sell"]
    mode: TradingMode
    status: OrderStatus
    amount_krw: int
    idempotency_key: str
    reason: str | None
    created_at: datetime

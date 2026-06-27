from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from app.domain.common.json import JsonObject


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    strategy: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class BacktestAssumptions:
    initial_cash_krw: float = 1_000_000.0
    transaction_fee_rate: float = 0.00015
    slippage_rate: float = 0.0005
    max_position_pct: float = 0.10
    max_sector_pct: float = 0.30
    max_daily_order_count: int = 10
    max_order_amount_krw: float = 100_000.0
    target_return_pct: float | None = None
    stop_loss_pct: float | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: str
    start: date
    end: date
    total_return: float
    cagr: float | None
    max_drawdown: float
    sharpe_like: float | None
    win_rate: float | None
    average_win: float
    average_loss: float
    turnover: float
    number_of_trades: int
    transaction_cost_krw: int
    blocked_reason_counts: dict[str, int] = field(default_factory=dict)
    assumptions: JsonObject = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_row(self) -> JsonObject:
        return {
            "strategy": self.strategy,
            "strategy_version": self.strategy,
            "period_start": self.start.isoformat(),
            "period_end": self.end.isoformat(),
            "total_return": self.total_return,
            "cagr": self.cagr,
            "max_drawdown": self.max_drawdown,
            "sharpe_like": self.sharpe_like,
            "win_rate": self.win_rate,
            "average_win": self.average_win,
            "average_loss": self.average_loss,
            "turnover": self.turnover,
            "number_of_trades": self.number_of_trades,
            "transaction_cost_krw": self.transaction_cost_krw,
            "blocked_reason_counts": dict(self.blocked_reason_counts),
            "assumptions": dict(self.assumptions),
            "created_at": self.created_at.isoformat(),
        }

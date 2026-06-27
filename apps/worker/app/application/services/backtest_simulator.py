from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from app.application.services.backtest_metrics import (
    average,
    cagr,
    max_drawdown,
    sharpe_like,
    win_rate,
)
from app.application.services.backtest_models import BacktestResult
from app.application.services.backtest_parsing import BacktestStrategy, DailyFeature
from app.domain.common.json import JsonObject


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    sector: str
    quantity: float
    avg_price: float
    target_sell_krw: float | None
    stop_loss_pct: float | None


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    side: str
    trade_date: date
    value_krw: float
    realized_pnl_krw: float | None


@dataclass(frozen=True, slots=True)
class TradeFill:
    symbol: str
    side: str
    trade_date: date
    value_krw: float
    fee_krw: float
    realized_pnl_krw: float | None


@dataclass(slots=True)
class SimulationLedger:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    closed_pnls: list[float] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    blocked_reasons: Counter[str] = field(default_factory=Counter)
    transaction_cost_krw: float = 0.0
    turnover_value_krw: float = 0.0


def simulate_backtest(
    strategy: BacktestStrategy,
    features_by_date: dict[date, list[DailyFeature]],
) -> BacktestResult:
    ledger = SimulationLedger(cash=strategy.assumptions.initial_cash_krw)
    previous_equity = strategy.assumptions.initial_cash_krw
    for trade_date in sorted(features_by_date):
        features = features_by_date[trade_date]
        prices = {feature.symbol: feature.close_price for feature in features}
        _apply_stop_target_exits(strategy, ledger, trade_date, prices)
        daily_order_count = 0
        for feature in features:
            daily_order_count = _evaluate_feature(strategy, ledger, feature, daily_order_count)
        equity = _equity(ledger, prices)
        ledger.equity_curve.append(equity)
        if previous_equity > 0:
            ledger.daily_returns.append((equity - previous_equity) / previous_equity)
        previous_equity = equity
    return _build_result(strategy, ledger, features_by_date)


def _evaluate_feature(
    strategy: BacktestStrategy,
    ledger: SimulationLedger,
    feature: DailyFeature,
    daily_order_count: int,
) -> int:
    if feature.close_price is None or feature.close_price <= 0:
        ledger.blocked_reasons["missing_price"] += 1
        return daily_order_count
    score = _weighted_score(strategy, feature)
    if score >= strategy.buy_threshold:
        return _buy(strategy, ledger, feature, daily_order_count)
    if score <= strategy.sell_threshold:
        _sell(strategy, ledger, feature.symbol, feature.trade_date, feature.close_price)
    return daily_order_count


def _weighted_score(strategy: BacktestStrategy, feature: DailyFeature) -> float:
    weights = strategy.weights
    total = weights.total()
    if total <= 0:
        return 0.0
    score = (
        weights.technical * feature.technical_score
        + weights.fundamental * feature.fundamental_score
        + weights.market_sector * feature.market_sector_score
        + weights.news_event * feature.news_event_score
        + weights.portfolio * feature.portfolio_score
    ) / total
    return max(0.0, min(1.0, score))


def _buy(
    strategy: BacktestStrategy,
    ledger: SimulationLedger,
    feature: DailyFeature,
    daily_order_count: int,
) -> int:
    assumptions = strategy.assumptions
    if feature.symbol in ledger.positions:
        ledger.blocked_reasons["existing_position"] += 1
        return daily_order_count
    if daily_order_count >= assumptions.max_daily_order_count:
        ledger.blocked_reasons["max_daily_order_count"] += 1
        return daily_order_count
    equity = _equity(ledger, {feature.symbol: feature.close_price})
    order_value = min(
        assumptions.max_order_amount_krw,
        ledger.cash / (1 + assumptions.transaction_fee_rate),
    )
    if order_value <= 0:
        ledger.blocked_reasons["cash_insufficient"] += 1
        return daily_order_count
    if (order_value / equity) > assumptions.max_position_pct:
        ledger.blocked_reasons["max_position_pct"] += 1
        return daily_order_count
    if (_sector_value(ledger, feature.sector) + order_value) / equity > assumptions.max_sector_pct:
        ledger.blocked_reasons["max_sector_pct"] += 1
        return daily_order_count
    close_price = feature.close_price
    if close_price is None:
        ledger.blocked_reasons["missing_price"] += 1
        return daily_order_count
    buy_price = close_price * (1 + assumptions.slippage_rate)
    fee = order_value * assumptions.transaction_fee_rate
    quantity = order_value / buy_price
    ledger.cash -= order_value + fee
    ledger.positions[feature.symbol] = Position(
        feature.symbol,
        feature.sector,
        quantity,
        buy_price,
        feature.target_sell_krw,
        feature.stop_loss_pct,
    )
    _record_trade(
        ledger,
        TradeFill(feature.symbol, "buy", feature.trade_date, order_value, fee, None),
    )
    return daily_order_count + 1


def _sell(
    strategy: BacktestStrategy,
    ledger: SimulationLedger,
    symbol: str,
    trade_date: date,
    close_price: float,
) -> None:
    position = ledger.positions.pop(symbol, None)
    if position is None:
        return
    sell_price = close_price * (1 - strategy.assumptions.slippage_rate)
    gross = sell_price * position.quantity
    fee = gross * strategy.assumptions.transaction_fee_rate
    pnl = gross - fee - (position.avg_price * position.quantity)
    ledger.cash += gross - fee
    ledger.closed_pnls.append(pnl)
    _record_trade(ledger, TradeFill(symbol, "sell", trade_date, gross, fee, pnl))


def _apply_stop_target_exits(
    strategy: BacktestStrategy,
    ledger: SimulationLedger,
    trade_date: date,
    prices: dict[str, float | None],
) -> None:
    for symbol, position in list(ledger.positions.items()):
        price = prices.get(symbol)
        if price is None:
            continue
        stop = (
            position.stop_loss_pct
            if position.stop_loss_pct is not None
            else strategy.assumptions.stop_loss_pct
        )
        target = strategy.assumptions.target_return_pct
        return_pct = (price - position.avg_price) / position.avg_price
        stop_hit = stop is not None and return_pct <= -abs(stop)
        target_price_hit = (
            position.target_sell_krw is not None and price >= position.target_sell_krw
        )
        target_return_hit = target is not None and return_pct >= abs(target)
        target_hit = target_price_hit or target_return_hit
        if stop_hit or target_hit:
            _sell(strategy, ledger, symbol, trade_date, price)


def _record_trade(ledger: SimulationLedger, fill: TradeFill) -> None:
    ledger.turnover_value_krw += fill.value_krw
    ledger.transaction_cost_krw += fill.fee_krw
    ledger.trades.append(
        Trade(
            fill.symbol,
            fill.side,
            fill.trade_date,
            fill.value_krw,
            fill.realized_pnl_krw,
        )
    )


def _build_result(
    strategy: BacktestStrategy,
    ledger: SimulationLedger,
    features_by_date: dict[date, list[DailyFeature]],
) -> BacktestResult:
    start = min(features_by_date) if features_by_date else date.today()
    end = max(features_by_date) if features_by_date else start
    initial_cash = strategy.assumptions.initial_cash_krw
    ending_equity = ledger.equity_curve[-1] if ledger.equity_curve else initial_cash
    total_return = (ending_equity - initial_cash) / initial_cash
    return BacktestResult(
        strategy=strategy.version,
        start=start,
        end=end,
        total_return=round(total_return, 6),
        cagr=cagr(total_return, start, end),
        max_drawdown=max_drawdown(ledger.equity_curve),
        sharpe_like=sharpe_like(ledger.daily_returns),
        win_rate=win_rate(ledger.closed_pnls),
        average_win=average([pnl for pnl in ledger.closed_pnls if pnl > 0]),
        average_loss=average([pnl for pnl in ledger.closed_pnls if pnl < 0]),
        turnover=round(ledger.turnover_value_krw / initial_cash, 6),
        number_of_trades=len(ledger.trades),
        transaction_cost_krw=round(ledger.transaction_cost_krw),
        blocked_reason_counts=dict(ledger.blocked_reasons),
        assumptions=_assumptions_row(strategy),
    )


def _assumptions_row(strategy: BacktestStrategy) -> JsonObject:
    assumptions = strategy.assumptions
    return {
        "initial_cash_krw": assumptions.initial_cash_krw,
        "transaction_fee_rate": assumptions.transaction_fee_rate,
        "slippage_rate": assumptions.slippage_rate,
        "max_position_pct": assumptions.max_position_pct,
        "max_sector_pct": assumptions.max_sector_pct,
        "max_daily_order_count": assumptions.max_daily_order_count,
        "max_order_amount_krw": assumptions.max_order_amount_krw,
        "target_return_pct": assumptions.target_return_pct,
        "stop_loss_pct": assumptions.stop_loss_pct,
    }


def _equity(ledger: SimulationLedger, prices: dict[str, float | None]) -> float:
    position_value = sum(
        position.quantity * (prices.get(symbol) or position.avg_price)
        for symbol, position in ledger.positions.items()
    )
    return ledger.cash + position_value


def _sector_value(ledger: SimulationLedger, sector: str) -> float:
    return sum(
        position.quantity * position.avg_price
        for position in ledger.positions.values()
        if position.sector == sector
    )

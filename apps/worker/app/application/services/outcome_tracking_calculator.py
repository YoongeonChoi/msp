from __future__ import annotations

from datetime import datetime

from app.application.services.outcome_tracking_models import (
    DecisionForOutcome,
    OutcomeRecord,
    OutcomeStatus,
    PaperOrderForOutcome,
    PricePoint,
    TradeSide,
    signed_return,
)
from app.application.services.outcome_tracking_parsing import number_from
from app.domain.common.json import JsonObject

TARGET_PRICE_KEYS = ("target_sell_price", "target_price", "take_profit_price")
STOP_PRICE_KEYS = ("stop_loss_price", "stop_price")
TARGET_PCT_KEYS = ("target_return_pct", "target_profit_pct", "take_profit_pct")
STOP_PCT_KEYS = ("stop_loss_pct", "stop_pct")


def calculate_outcome(
    decision: DecisionForOutcome,
    orders_by_decision: dict[str, PaperOrderForOutcome],
    prices_by_symbol: dict[str, list[PricePoint]],
    calculated_at: datetime,
) -> OutcomeRecord:
    linked_order = orders_by_decision.get(decision.decision_id)
    if decision.side is None:
        return skipped(decision, linked_order, "non_trade_action", calculated_at)
    if decision.decided_at is None:
        return skipped(decision, linked_order, "missing_decided_at", calculated_at)
    if decision.price_at_decision is None or decision.price_at_decision <= 0:
        return skipped(decision, linked_order, "missing_price_at_decision", calculated_at)

    future_prices = prices_after(prices_by_symbol.get(decision.symbol, []), decision.decided_at)
    returns = [
        signed_return(decision.side, decision.price_at_decision, point.close_price)
        for point in future_prices[:20]
    ]
    status, reason = status_for_future_prices(future_prices)
    return OutcomeRecord(
        decision_id=decision.decision_id,
        order_id=linked_order.order_id if linked_order is not None else None,
        symbol=decision.symbol,
        price_at_decision=decision.price_at_decision,
        return_1d=return_at(returns, 1),
        return_5d=return_at(returns, 5),
        return_20d=return_at(returns, 20),
        max_drawdown_20d=max_drawdown(returns),
        hit_target=hit_threshold(
            decision.side,
            decision.price_at_decision,
            future_prices[:20],
            decision.feature_snapshot,
            target=True,
        ),
        hit_stop=hit_threshold(
            decision.side,
            decision.price_at_decision,
            future_prices[:20],
            decision.feature_snapshot,
            target=False,
        ),
        realized_pnl_krw=realized_pnl(
            decision.side, decision.price_at_decision, linked_order, future_prices
        ),
        status=status,
        reason=reason,
        calculated_at=calculated_at,
    )


def prices_after(points: list[PricePoint], decided_at: datetime) -> list[PricePoint]:
    decision_date = decided_at.date()
    return [point for point in points if point.trade_date > decision_date]


def status_for_future_prices(points: list[PricePoint]) -> tuple[OutcomeStatus, str | None]:
    if not points:
        return OutcomeStatus.PENDING, "missing_future_price_data"
    if len(points) < 20:
        return OutcomeStatus.PARTIAL, "insufficient_future_data"
    return OutcomeStatus.COMPLETE, None


def return_at(returns: list[float], horizon: int) -> float | None:
    if len(returns) < horizon:
        return None
    return round(returns[horizon - 1], 6)


def max_drawdown(returns: list[float]) -> float | None:
    if not returns:
        return None
    return round(min(0.0, min(returns)), 6)


def hit_threshold(
    side: TradeSide,
    entry_price: float,
    points: list[PricePoint],
    feature_snapshot: JsonObject,
    *,
    target: bool,
) -> bool | None:
    price_keys = TARGET_PRICE_KEYS if target else STOP_PRICE_KEYS
    pct_keys = TARGET_PCT_KEYS if target else STOP_PCT_KEYS
    threshold_price = number_from(feature_snapshot, price_keys)
    threshold_pct = number_from(feature_snapshot, pct_keys)
    if threshold_price is None and threshold_pct is None:
        return None
    if threshold_price is not None:
        return hit_price_threshold(side, points, threshold_price, target=target)
    if threshold_pct is None:
        return None
    required = abs(threshold_pct)
    returns = [signed_return(side, entry_price, point.close_price) for point in points]
    return any(value >= required for value in returns) if target else any(
        value <= -required for value in returns
    )


def hit_price_threshold(
    side: TradeSide, points: list[PricePoint], threshold_price: float, *, target: bool
) -> bool:
    prices = [point.close_price for point in points]
    if side == TradeSide.BUY:
        return any(price >= threshold_price for price in prices) if target else any(
            price <= threshold_price for price in prices
        )
    return any(price <= threshold_price for price in prices) if target else any(
        price >= threshold_price for price in prices
    )


def realized_pnl(
    side: TradeSide,
    entry_price: float,
    order: PaperOrderForOutcome | None,
    future_prices: list[PricePoint],
) -> int | None:
    if order is None or len(future_prices) < 20:
        return None
    quantity = paper_quantity(order, entry_price)
    if quantity is None or quantity <= 0:
        return None
    exit_price = future_prices[19].close_price
    pnl = signed_return(side, entry_price, exit_price) * entry_price * quantity
    return round(pnl)


def paper_quantity(order: PaperOrderForOutcome, entry_price: float) -> float | None:
    if order.quantity is not None:
        return order.quantity
    order_price = order.price or entry_price
    if order.amount_krw is None or order_price <= 0:
        return None
    return order.amount_krw / order_price


def skipped(
    decision: DecisionForOutcome,
    order: PaperOrderForOutcome | None,
    reason: str,
    calculated_at: datetime,
) -> OutcomeRecord:
    return OutcomeRecord(
        decision_id=decision.decision_id,
        order_id=order.order_id if order is not None else None,
        symbol=decision.symbol,
        price_at_decision=decision.price_at_decision,
        return_1d=None,
        return_5d=None,
        return_20d=None,
        max_drawdown_20d=None,
        hit_target=None,
        hit_stop=None,
        realized_pnl_krw=None,
        status=OutcomeStatus.SKIPPED,
        reason=reason,
        calculated_at=calculated_at,
    )

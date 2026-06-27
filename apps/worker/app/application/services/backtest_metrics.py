from __future__ import annotations

from datetime import date
from math import pow, sqrt


def max_drawdown(equity_curve: list[float]) -> float:
    peak = 0.0
    drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = min(drawdown, (equity - peak) / peak)
    return round(drawdown, 6)


def sharpe_like(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    return round((mean / sqrt(variance)) * sqrt(252), 6)


def cagr(total_return: float, start: date, end: date) -> float | None:
    days = (end - start).days
    if days < 365:
        return None
    return round(pow(1 + total_return, 365 / days) - 1, 6)


def win_rate(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    return round(sum(1 for pnl in pnls if pnl > 0) / len(pnls), 6)


def average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)

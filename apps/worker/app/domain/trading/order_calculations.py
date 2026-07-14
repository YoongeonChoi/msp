from __future__ import annotations


def calculate_order_quantity(amount_krw: int, limit_price_krw: int) -> int | None:
    """Return the whole-share quantity for a limit order, or None when invalid."""
    if amount_krw <= 0 or limit_price_krw <= 0:
        return None
    quantity = amount_krw // limit_price_krw
    return quantity if quantity > 0 else None


def calculate_order_notional(amount_krw: int, limit_price_krw: int) -> int | None:
    quantity = calculate_order_quantity(amount_krw, limit_price_krw)
    if quantity is None:
        return None
    return quantity * limit_price_krw

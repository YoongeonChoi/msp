from app.domain.trading.order_calculations import (
    calculate_order_notional,
    calculate_order_quantity,
)


def test_order_quantity_matches_whole_share_limit_order_math() -> None:
    assert calculate_order_quantity(100_000, 75_000) == 1
    assert calculate_order_notional(100_000, 75_000) == 75_000


def test_order_quantity_rejects_invalid_or_too_small_amounts() -> None:
    assert calculate_order_quantity(74_999, 75_000) is None
    assert calculate_order_quantity(100_000, 0) is None
    assert calculate_order_quantity(0, 75_000) is None
    assert calculate_order_notional(74_999, 75_000) is None

from app.infrastructure.idempotency import build_idempotency_key


def test_idempotency_key_is_stable_for_same_logical_order() -> None:
    first = build_idempotency_key("paper", "decision-1", "005930", "buy", 100_000)
    second = build_idempotency_key("paper", "decision-1", "005930", "buy", 100_000)

    assert first == second


def test_idempotency_key_differs_for_distinct_orders() -> None:
    first = build_idempotency_key("paper", "decision-1", "005930", "buy", 100_000)
    second = build_idempotency_key("paper", "decision-2", "005930", "buy", 100_000)

    assert first != second


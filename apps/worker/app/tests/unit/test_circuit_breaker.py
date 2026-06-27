from datetime import UTC, datetime, timedelta

from app.infrastructure.circuit_breaker import CircuitBreaker


def test_circuit_breaker_opens_and_half_opens_after_recovery_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    breaker = CircuitBreaker(provider="toss", failure_threshold=2, recovery_seconds=10)

    breaker.record_failure(now)
    breaker.record_failure(now)

    assert breaker.state == "open"
    assert breaker.can_attempt(now + timedelta(seconds=5)) is False
    assert breaker.can_attempt(now + timedelta(seconds=11)) is True
    assert str(breaker.state) == "half_open"

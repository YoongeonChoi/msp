from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.execution_v2.models import (
    AccountingTransaction,
    ExecutionCostSchedule,
    ExecutionInvariantError,
    ExecutionObservation,
    LedgerPosting,
    PaperExecutionEvidence,
    PaperFill,
    build_provider_observation_sha256,
    build_semantic_key,
    canonical_semantic_key_payload,
    next_full_minute,
    validate_execution_environment,
)


def test_semantic_key_uses_signal_window_and_policy_identity() -> None:
    valid_from = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    valid_until = valid_from + timedelta(minutes=5)
    first = build_semantic_key(
        account_id="account-a",
        environment="paper",
        strategy_version_id="strategy-v1",
        symbol="005930",
        side="buy",
        signal_valid_from=valid_from,
        signal_valid_until=valid_until,
        execution_policy_version="limit-day-v1",
    )
    second = build_semantic_key(
        account_id="account-a",
        environment="paper",
        strategy_version_id="strategy-v1",
        symbol="005930",
        side="buy",
        signal_valid_from=valid_from,
        signal_valid_until=valid_until,
        execution_policy_version="limit-day-v1",
    )
    changed = build_semantic_key(
        account_id="account-a",
        environment="paper",
        strategy_version_id="strategy-v1",
        symbol="005930",
        side="buy",
        signal_valid_from=valid_from,
        signal_valid_until=valid_until,
        execution_policy_version="limit-day-v2",
    )

    assert first == second
    assert first != changed
    assert len(first) == 64


def test_semantic_key_matches_cross_language_golden_vector() -> None:
    valid_from = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    valid_until = valid_from + timedelta(minutes=5)
    expected_payload = (
        b'{"account_id":"account-a","environment":"paper",'
        b'"execution_policy_version":"limit-day-v1","side":"buy",'
        b'"signal_valid_from":"2026-07-14T09:00:00+00:00",'
        b'"signal_valid_until":"2026-07-14T09:05:00+00:00",'
        b'"strategy_version_id":"strategy-v1","symbol":"005930"}'
    )
    inputs = {
        "account_id": "account-a",
        "environment": "paper",
        "strategy_version_id": "strategy-v1",
        "symbol": "005930",
        "side": "buy",
        "signal_valid_from": valid_from,
        "signal_valid_until": valid_until,
        "execution_policy_version": "limit-day-v1",
    }

    assert canonical_semantic_key_payload(**inputs) == expected_payload  # type: ignore[arg-type]
    assert build_semantic_key(**inputs) == (  # type: ignore[arg-type]
        "113ae28df2825d123e6c7c627e8d10874d22f45dcc6869ef2aa138438a0458ed"
    )


def test_production_execution_environment_is_rejected() -> None:
    with pytest.raises(
        ExecutionInvariantError,
        match="unsupported_or_production_execution_environment",
    ):
        validate_execution_environment("live")


def test_next_full_minute_always_excludes_decision_minute() -> None:
    decision_at = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)

    assert next_full_minute(decision_at) == datetime(2026, 7, 14, 9, 1, tzinfo=UTC)


def test_cost_schedule_requires_evidence_and_valid_window() -> None:
    effective_from = datetime(2026, 7, 14, tzinfo=UTC)
    schedule = ExecutionCostSchedule(
        version="fees-2026-07",
        effective_from=effective_from,
        effective_until=effective_from + timedelta(days=1),
        evidence_sha256="a" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )

    assert schedule.covers(
        effective_from + timedelta(hours=1),
        effective_from + timedelta(hours=2),
    )
    assert not schedule.covers(
        effective_from - timedelta(seconds=1),
        effective_from + timedelta(hours=2),
    )


def test_settlement_calendar_uses_korean_trade_date_at_utc_boundary() -> None:
    filled_at = datetime(2026, 7, 14, 15, 30, tzinfo=UTC)
    evidence = PaperExecutionEvidence(
        version="paper-evidence-v1",
        execution_policy_version="limit-day-v1",
        effective_from=filled_at - timedelta(days=1),
        effective_until=filled_at + timedelta(days=4),
        tick_rule_version="krx-tick-v1",
        tick_size_krw=1,
        tick_rule_evidence_sha256="b" * 64,
        volume_source="verified-bars",
        volume_unit="shares",
        volume_evidence_sha256="c" * 64,
        corporate_action_status="not_required",
        corporate_action_evidence_sha256="d" * 64,
        market_calendar_version="krx-calendar-v1",
        market_calendar_status="open_sessions_verified",
        market_calendar_evidence_sha256="e" * 64,
        open_session_dates=(
            date(2026, 7, 14),
            date(2026, 7, 15),
            date(2026, 7, 16),
            date(2026, 7, 17),
        ),
    )

    assert evidence.settlement_date_for(filled_at, 1) == date(2026, 7, 16)


def test_fill_rejects_settlement_before_korean_trade_date() -> None:
    with pytest.raises(ExecutionInvariantError, match="fill_settlement_date_precedes_fill"):
        PaperFill(
            sequence=1,
            filled_at=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
            quantity=1,
            price_krw=10_000,
            commission_krw=2,
            tax_krw=0,
            settlement_date=date(2026, 7, 14),
        )


def test_execution_observation_is_immutable() -> None:
    observation = ExecutionObservation.create(
        intent_id="intent-a",
        sequence=1,
        status="open",
        observed_at=datetime(2026, 7, 14, 9, 1, tzinfo=UTC),
        provider_order_id="paper:intent-a",
        provider_execution_id=None,
        cumulative_quantity=0,
        cumulative_gross_krw=0,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
    )

    with pytest.raises(FrozenInstanceError):
        observation.status = "filled"  # type: ignore[misc]


def test_provider_observation_hash_matches_database_contract() -> None:
    observed_at = datetime(2026, 7, 14, 9, 1, 0, 123456, tzinfo=UTC)

    digest = build_provider_observation_sha256(
        intent_id="00000000-0000-0000-0000-000000000001",
        sequence=2,
        status="partial_filled",
        provider_order_id="paper-order-1",
        provider_execution_id="paper-execution-1",
        observed_at=observed_at,
        cumulative_quantity=3,
        cumulative_gross_krw=29_970,
        cumulative_commission_krw=4,
        cumulative_tax_krw=53,
        last_fill_quantity=2,
        last_fill_price_krw=9_990,
        last_fill_settlement_date=observed_at.date(),
        reason="partial_fill",
    )

    assert digest == (
        "7cab37831e423e13b1adcd93054fe6882bcf30075a21d78bdfac79cb0dd1efe8"
    )


def test_unbalanced_accounting_transaction_is_rejected() -> None:
    with pytest.raises(ExecutionInvariantError, match="accounting_transaction_is_not_balanced"):
        AccountingTransaction(
            id="transaction-a",
            intent_id="intent-a",
            observation_sequence=1,
            posted_at=datetime(2026, 7, 14, 9, 1, tzinfo=UTC),
            postings=(
                LedgerPosting("cash", debit_krw=100),
                LedgerPosting("proceeds", credit_krw=99),
            ),
        )

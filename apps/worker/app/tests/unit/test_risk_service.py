from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from app.application.services.risk_service import RiskService
from app.domain.common.time import now_utc
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.entities import AccountState, BotSettings, Quote, Signal


def _risk_input(settings: BotSettings | None = None) -> RiskInput:
    now = now_utc()
    return RiskInput(
        settings=settings or BotSettings(),
        signal=Signal(
            symbol="005930",
            action="buy",
            final_score=0.8,
            confidence=0.8,
            order_amount_krw=100_000,
            sector="technology",
        ),
        account_state=AccountState(
            synced=True,
            cash_krw=1_000_000,
            equity_krw=10_000_000,
            daily_loss_pct=0.0,
            daily_order_count=0,
            synced_at=now,
        ),
        quote=Quote(symbol="005930", price_krw=75_000, as_of=now),
        now=now,
        provider_health={"supabase": True, "toss": True},
        market_open=True,
        existing_position_pct=0.0,
        sector_position_pct=0.0,
        critical_news_risk=False,
        liquidity_ok=True,
        volatility_ok=True,
        cooldown_active=False,
        duplicate_order=False,
        strategy_version_id=uuid4(),
    )


def test_live_order_is_blocked_by_default() -> None:
    result = RiskService().evaluate_live_order(_risk_input())

    assert result.allowed is False
    assert "bot_disabled" in result.reasons
    assert "mode_not_live" in result.reasons
    assert "live_order_allowed_false" in result.reasons


def test_live_order_requires_all_gates() -> None:
    settings = BotSettings(enabled=True, mode="live", live_order_allowed=True)
    result = RiskService().evaluate_live_order(_risk_input(settings))

    assert result.allowed is True
    assert result.reasons == []


def test_stale_quote_blocks_live_order() -> None:
    settings = BotSettings(enabled=True, mode="live", live_order_allowed=True)
    risk_input = _risk_input(settings)
    assert risk_input.quote is not None
    stale_quote = replace(risk_input.quote, as_of=risk_input.now.replace(year=2020))

    result = RiskService().evaluate_live_order(replace(risk_input, quote=stale_quote))

    assert result.allowed is False
    assert "stale_quote" in result.reasons


def test_critical_news_blocks_new_buy() -> None:
    settings = BotSettings(enabled=True, mode="live", live_order_allowed=True)
    result = RiskService().evaluate_live_order(
        replace(_risk_input(settings), critical_news_risk=True)
    )

    assert result.allowed is False
    assert "critical_negative_news_risk" in result.reasons


def test_critical_news_blocks_paper_buy() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    result = RiskService().evaluate_paper_order(
        replace(_risk_input(settings), critical_news_risk=True)
    )

    assert result.allowed is False
    assert "critical_negative_news_risk" in result.reasons


def test_missing_quote_blocks_paper_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    result = RiskService().evaluate_paper_order(replace(_risk_input(settings), quote=None))

    assert result.allowed is False
    assert "missing_quote" in result.reasons


def test_provider_health_bad_blocks_paper_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    result = RiskService().evaluate_paper_order(
        replace(_risk_input(settings), provider_health={"supabase": True, "toss": False})
    )

    assert result.allowed is False
    assert "critical_provider_unhealthy:toss" in result.reasons


def test_position_limits_block_paper_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    result = RiskService().evaluate_paper_order(
        replace(_risk_input(settings), existing_position_pct=settings.max_position_pct)
    )

    assert result.allowed is False
    assert "max_position_pct_exceeded" in result.reasons


def test_sector_limits_block_paper_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    result = RiskService().evaluate_paper_order(
        replace(_risk_input(settings), sector_position_pct=settings.max_sector_pct)
    )

    assert result.allowed is False
    assert "max_sector_pct_exceeded" in result.reasons


def test_daily_order_count_blocks_paper_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    base_input = _risk_input(settings)
    assert base_input.account_state is not None
    account = replace(base_input.account_state, daily_order_count=settings.max_daily_order_count)

    result = RiskService().evaluate_paper_order(replace(base_input, account_state=account))

    assert result.allowed is False
    assert "max_daily_order_count_exceeded" in result.reasons


def test_daily_loss_blocks_paper_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    base_input = _risk_input(settings)
    assert base_input.account_state is not None
    account = replace(base_input.account_state, daily_loss_pct=settings.max_daily_loss_pct)

    result = RiskService().evaluate_paper_order(replace(base_input, account_state=account))

    assert result.allowed is False
    assert "max_daily_loss_pct_exceeded" in result.reasons


def test_max_order_amount_blocks_paper_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    base_input = _risk_input(settings)
    signal = replace(base_input.signal, order_amount_krw=settings.max_order_amount_krw + 1)

    result = RiskService().evaluate_paper_order(replace(base_input, signal=signal))

    assert result.allowed is False
    assert "max_order_amount_krw_exceeded" in result.reasons


def test_missing_strategy_version_blocks_order() -> None:
    settings = BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    result = RiskService().evaluate_paper_order(
        replace(_risk_input(settings), strategy_version_id=None)
    )

    assert result.allowed is False
    assert "missing_strategy_version" in result.reasons


def test_invalid_settings_block_order() -> None:
    settings = BotSettings(
        enabled=True,
        mode="paper",
        live_order_allowed=False,
        max_order_amount_krw=100_000_001,
    )
    result = RiskService().evaluate_paper_order(_risk_input(settings))

    assert result.allowed is False
    assert "invalid_max_order_amount_krw" in result.reasons

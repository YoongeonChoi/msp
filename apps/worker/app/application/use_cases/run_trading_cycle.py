from __future__ import annotations

from datetime import datetime
from typing import assert_never
from uuid import uuid4

from app.application.ports.market_data_port import MarketDataPort
from app.application.ports.repository_port import RepositoryPort
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.risk_service import RiskService
from app.application.services.signal_service import WeightedFactorStrategyV1
from app.domain.common.time import now_utc
from app.domain.risk.value_objects import RiskInput
from app.domain.strategy.entities import FeatureVector, StrategyContext
from app.domain.trading.entities import AccountState, DecisionSnapshot, Signal
from app.domain.trading.policies import settings_validation_reasons
from app.infrastructure.idempotency import build_idempotency_key


class RunTradingCycle:
    def __init__(
        self,
        repository: RepositoryPort,
        market_data: MarketDataPort,
        health_service: HealthService,
        execution_service: ExecutionService,
        risk_service: RiskService,
        feature_service: FeatureService,
    ) -> None:
        self.repository = repository
        self.market_data = market_data
        self.health_service = health_service
        self.execution_service = execution_service
        self.risk_service = risk_service
        self.feature_service = feature_service
        self.strategy = WeightedFactorStrategyV1()

    async def execute(self) -> None:
        cycle_id = uuid4()
        now = now_utc()
        await self.repository.record_heartbeat("ok", {"cycle_id": str(cycle_id)})
        settings = await self.repository.load_bot_settings()
        if not settings.enabled:
            await self.health_service.check()
            return
        settings_errors = settings_validation_reasons(settings)
        if settings_errors:
            await self.repository.record_engine_event(
                "warning", "settings", "invalid_settings", {"reasons": settings_errors}
            )
            return
        provider_health = await self.health_service.check()
        if provider_health.get("supabase") is not True or provider_health.get("toss") is not True:
            provider_details: dict[str, object] = {
                provider: healthy for provider, healthy in provider_health.items()
            }
            await self.repository.record_engine_event(
                "warning", "provider_health", "critical provider unhealthy", provider_details
            )
            return
        market_open = await self.market_data.is_market_open()
        if market_open is not True:
            await self.repository.record_engine_event(
                "info", "market_calendar", "market_closed_or_unknown", {"market_open": market_open}
            )
            return
        symbols = await self.repository.load_enabled_watchlist()
        quotes = await self.market_data.get_quotes(symbols)
        strategy_version = await self.repository.load_active_strategy_version()
        if strategy_version is None:
            await self.repository.record_engine_event(
                "warning", "strategy", "missing_strategy_version", {}
            )
            return
        account = AccountState(
            synced=True,
            cash_krw=10_000_000,
            equity_krw=10_000_000,
            daily_loss_pct=0.0,
            daily_order_count=0,
            synced_at=now,
        )
        for symbol in symbols:
            quote = quotes.get(symbol)
            if quote is None:
                await self.repository.record_engine_event(
                    "warning", "market_data", "missing_quote", {"symbol": symbol}
                )
                continue
            features = self.feature_service.build_mock_features(symbol, quote)
            signal = self.strategy.score(
                features,
                StrategyContext(
                    strategy_version_id=strategy_version.id,
                    weights=strategy_version.weights,
                    buy_threshold=strategy_version.buy_threshold,
                    sell_threshold=strategy_version.sell_threshold,
                    order_amount_krw=settings.max_order_amount_krw,
                    sector="technology",
                ),
            )
            paper_idempotency_key = paper_signal_idempotency_key(
                signal, now, strategy_version.version
            )
            duplicate_order = (
                paper_idempotency_key is not None
                and await self.repository.idempotency_key_exists(paper_idempotency_key)
            )
            risk_input = RiskInput(
                settings=settings,
                signal=signal,
                account_state=account,
                quote=quote,
                now=now,
                provider_health=provider_health,
                market_open=market_open,
                existing_position_pct=0.0,
                sector_position_pct=0.0,
                critical_news_risk=False,
                liquidity_ok=True,
                volatility_ok=True,
                cooldown_active=False,
                duplicate_order=duplicate_order,
                strategy_version_id=strategy_version.id,
            )
            match settings.mode:
                case "paper":
                    risk_result = self.risk_service.evaluate_paper_order(risk_input)
                case "live":
                    risk_result = self.risk_service.evaluate_live_order(risk_input)
                case unreachable:
                    assert_never(unreachable)
            snapshot = DecisionSnapshot.create(
                cycle_id=cycle_id,
                signal=signal,
                strategy_version_id=strategy_version.id,
                created_at=now,
                feature_snapshot=feature_snapshot_from_signal(features, signal),
                risk_snapshot=risk_result.to_dict(),
            )
            await self.repository.persist_decision_snapshot(snapshot)
            if paper_idempotency_key is None:
                continue
            match settings.mode:
                case "paper":
                    await self.execution_service.create_paper_order(
                        snapshot, risk_result, paper_idempotency_key
                    )
                case "live":
                    await self.execution_service.propose_live_order(snapshot, risk_input)
                case unreachable:
                    assert_never(unreachable)


def paper_signal_idempotency_key(
    signal: Signal,
    created_at: datetime,
    strategy_version: str,
) -> str | None:
    match signal.action:
        case "hold":
            return None
        case "buy" | "sell":
            cooldown_bucket = created_at.strftime("%Y%m%d%H")
            return build_idempotency_key(
                mode="paper",
                decision_id=f"{cooldown_bucket}:{strategy_version}",
                symbol=signal.symbol,
                action=signal.action,
                amount_krw=signal.order_amount_krw,
            )
        case unreachable:
            assert_never(unreachable)


def feature_snapshot_from_signal(
    features: FeatureVector,
    signal: Signal,
) -> dict[str, object]:
    return {
        "technical_score": features.technical_score,
        "fundamental_score": features.fundamental_score,
        "market_sector_score": features.market_sector_score,
        "news_event_score": features.news_event_score,
        "portfolio_score": features.portfolio_score,
        "final_score": signal.final_score,
        "raw": features.raw,
    }

from __future__ import annotations

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
from app.domain.strategy.entities import StrategyContext
from app.domain.trading.entities import AccountState, DecisionSnapshot
from app.domain.trading.policies import validate_settings
from app.domain.trading.value_objects import StrategyWeights


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
        validate_settings(settings)
        if not settings.enabled:
            await self.repository.record_engine_event(
                "info", "trading_loop", "bot_disabled_no_orders_created", {}
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
        strategy_id = await self.repository.load_active_strategy_version_id()
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
                    strategy_version_id=strategy_id,
                    weights=StrategyWeights(),
                    order_amount_krw=settings.max_order_amount_krw,
                    sector="technology",
                ),
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
                duplicate_order=False,
            )
            risk_result = self.risk_service.evaluate_live_order(risk_input)
            snapshot = DecisionSnapshot.create(
                cycle_id=cycle_id,
                signal=signal,
                strategy_version_id=strategy_id,
                created_at=now,
                feature_snapshot=features.raw,
                risk_snapshot=risk_result.to_dict(),
            )
            await self.repository.persist_decision_snapshot(snapshot)
            if signal.action not in {"buy", "sell"}:
                continue
            if settings.mode == "paper":
                await self.execution_service.create_paper_order(snapshot)
            else:
                await self.execution_service.propose_live_order(snapshot, risk_input)

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import assert_never
from uuid import uuid4

from app.application.ports.alert_port import AlertNotifierPort
from app.application.ports.broker_port import BrokerPort
from app.application.ports.market_data_port import MarketDataPort
from app.application.ports.repository_port import RepositoryPort
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.order_reconciliation_service import OrderReconciliationService
from app.application.services.risk_service import RiskService
from app.application.services.signal_service import WeightedFactorStrategyV1
from app.domain.common.errors import ProviderError
from app.domain.common.time import KST, now_utc
from app.domain.risk.value_objects import RiskInput
from app.domain.strategy.entities import FeatureVector, StrategyContext
from app.domain.trading.entities import AccountState, DecisionSnapshot, Quote, Signal
from app.domain.trading.policies import settings_validation_reasons
from app.infrastructure.idempotency import build_idempotency_key


class RunTradingCycle:
    def __init__(
        self,
        repository: RepositoryPort,
        broker: BrokerPort,
        market_data: MarketDataPort,
        health_service: HealthService,
        execution_service: ExecutionService,
        risk_service: RiskService,
        feature_service: FeatureService,
        order_reconciliation_service: OrderReconciliationService | None = None,
        alert_notifier: AlertNotifierPort | None = None,
        live_system_order_count_scope_accepted: bool = False,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.market_data = market_data
        self.health_service = health_service
        self.execution_service = execution_service
        self.risk_service = risk_service
        self.feature_service = feature_service
        self.order_reconciliation_service = order_reconciliation_service
        self.alert_notifier = alert_notifier
        self.live_system_order_count_scope_accepted = live_system_order_count_scope_accepted
        self.strategy = WeightedFactorStrategyV1()

    async def execute(self) -> None:
        cycle_id = uuid4()
        now = now_utc()
        await self.repository.record_heartbeat("ok", {"cycle_id": str(cycle_id)})
        if self.order_reconciliation_service is not None:
            await self.order_reconciliation_service.reconcile_live_orders()
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
        if settings.mode == "live" and await self._has_pending_live_reconciliation():
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
        account = await self._account_state_for_mode(settings.mode, now)
        for symbol in symbols:
            quote = quotes.get(symbol)
            if quote is None:
                await self.repository.record_engine_event(
                    "warning", "market_data", "missing_quote", {"symbol": symbol}
                )
                continue
            features = await self._features_for_mode(settings.mode, symbol, quote)
            if (
                settings.mode == "live"
                and features.raw.get("live_trading_ready") is not True
            ):
                await self._record_critical_event(
                    "live_features",
                    "live_feature_snapshot_not_ready",
                    {
                        "symbol": symbol,
                        "reasons": _feature_unready_reasons(features),
                    },
                )
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

    async def _account_state_for_mode(
        self,
        mode: str,
        now: datetime,
    ) -> AccountState | None:
        match mode:
            case "paper":
                return AccountState(
                    synced=True,
                    cash_krw=10_000_000,
                    equity_krw=10_000_000,
                    daily_loss_pct=0.0,
                    daily_order_count=0,
                    synced_at=now,
                )
            case "live":
                try:
                    account = await self.broker.get_account_state(now)
                except ProviderError as exc:
                    await self._record_critical_event(
                        "live_account",
                        "live_account_state_sync_failed",
                        {"provider": exc.provider, "reason": exc.safe_message},
                    )
                    return None
                if not self.live_system_order_count_scope_accepted:
                    await self._record_critical_event(
                        "live_account",
                        "live_external_order_history_scope_not_accepted",
                        {
                            "required_env": "LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED",
                            "accepted": False,
                            "daily_order_count_scope": "system_created_live_orders_only",
                        },
                    )
                    return replace(account, daily_order_count_verified=False)
                try:
                    day_start, day_end = kst_day_window(now)
                    daily_order_count = (
                        await self.repository.count_system_live_orders_created_between(
                            day_start,
                            day_end,
                        )
                    )
                except Exception as exc:
                    await self._record_critical_event(
                        "live_account",
                        "live_system_order_count_sync_failed",
                        {"reason": type(exc).__name__},
                    )
                    return replace(account, daily_order_count_verified=False)
                return replace(
                    account,
                    daily_order_count=daily_order_count,
                    daily_order_count_verified=True,
                )
            case _:
                return None

    async def _has_pending_live_reconciliation(self) -> bool:
        pending_orders = await self.repository.load_live_orders_for_reconciliation(limit=50)
        if not pending_orders:
            return False
        status_counts: dict[str, int] = {}
        symbols: set[str] = set()
        for order in pending_orders:
            status_counts[order.status] = status_counts.get(order.status, 0) + 1
            symbols.add(order.symbol)
        await self._record_critical_event(
            "live_reconciliation",
            "live_pending_reconciliation_blocks_new_decisions",
            {
                "pending_order_count": len(pending_orders),
                "statuses": status_counts,
                "symbols": sorted(symbols),
            },
        )
        return True

    async def _features_for_mode(
        self,
        mode: str,
        symbol: str,
        quote: Quote,
    ) -> FeatureVector:
        match mode:
            case "paper":
                return self.feature_service.build_mock_features(symbol, quote)
            case "live":
                return await self.feature_service.build_live_features(symbol, quote)
            case _:
                return self.feature_service.build_mock_features(symbol, quote)

    async def _record_critical_event(
        self,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> None:
        await self.repository.record_engine_event("critical", component, message, details)
        if self.alert_notifier is not None:
            await self.alert_notifier.notify_engine_event(
                "critical",
                component,
                message,
                details,
            )


def kst_day_window(now: datetime) -> tuple[datetime, datetime]:
    day_start = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


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


def _feature_unready_reasons(features: FeatureVector) -> list[str]:
    reasons = features.raw.get("feature_unready_reasons")
    if not isinstance(reasons, list):
        return ["feature_snapshot_not_live_ready"]
    return [reason for reason in reasons if isinstance(reason, str)]

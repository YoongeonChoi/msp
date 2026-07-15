from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import assert_never
from uuid import UUID, uuid4

from app.application.ports.alert_port import AlertNotifierPort
from app.application.ports.broker_port import BrokerPort
from app.application.ports.market_data_port import MarketDataPort
from app.application.ports.repository_port import RepositoryPort
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.order_reconciliation_service import OrderReconciliationService
from app.application.services.portfolio_service import PortfolioService
from app.application.services.risk_service import RiskService
from app.application.services.signal_service import WeightedFactorStrategyV1
from app.domain.common.errors import ProviderError
from app.domain.common.time import KST, now_utc
from app.domain.portfolio.entities import Position
from app.domain.risk.value_objects import RiskInput
from app.domain.strategy.entities import FeatureVector, StrategyContext
from app.domain.trading.entities import (
    AccountState,
    BotSettings,
    DecisionSnapshot,
    Quote,
    Signal,
)
from app.domain.trading.policies import settings_validation_reasons
from app.infrastructure.idempotency import build_idempotency_key
from app.infrastructure.release_metadata import worker_heartbeat_details

UNVERIFIED_SECTOR_SOURCES = {
    "",
    "fixture",
    "krx_mock",
    "missing_live_sector_provider",
    "mock",
    "mock_static",
    "unconfigured",
    "unknown",
}
UNKNOWN_SECTORS = {"", "unknown", "unclassified", "unconfigured"}
LIVE_ORDER_COOLDOWN = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class CycleRiskEvidence:
    sector: str | None
    existing_position_pct: float | None
    sector_position_pct: float | None
    available_position_quantity: int | None
    critical_news_risk: bool | None
    liquidity_ok: bool | None
    volatility_ok: bool | None
    source: str
    position_sync_verified: bool
    sector_exposure_verified: bool


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
        portfolio_service: PortfolioService | None = None,
        alert_notifier: AlertNotifierPort | None = None,
        live_system_order_count_scope_accepted: bool = False,
        shutdown_requested: Callable[[], bool] | None = None,
        mock_providers: bool | None = None,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.market_data = market_data
        self.health_service = health_service
        self.execution_service = execution_service
        self.risk_service = risk_service
        self.feature_service = feature_service
        self.order_reconciliation_service = order_reconciliation_service
        self.portfolio_service = portfolio_service
        self.alert_notifier = alert_notifier
        self.live_system_order_count_scope_accepted = live_system_order_count_scope_accepted
        self.shutdown_requested = shutdown_requested or (lambda: False)
        self.mock_providers = mock_providers
        self.strategy = WeightedFactorStrategyV1()

    async def execute(self) -> None:
        cycle_id = uuid4()
        started_at = now_utc()
        heartbeat_details = worker_heartbeat_details(
            str(cycle_id),
            deployment_lock=False,
            deployment_target_sha=None,
            mock_providers=self.mock_providers,
        ) | {
            "started_at": started_at.isoformat(),
            "checkpoint": "cycle_started",
        }
        await self.repository.record_heartbeat(
            "warning",
            heartbeat_details,
        )
        try:
            settings = await self.repository.load_bot_settings()
            heartbeat_details = worker_heartbeat_details(
                str(cycle_id),
                deployment_lock=settings.deployment_lock,
                deployment_target_sha=settings.deployment_target_sha,
                mock_providers=self.mock_providers,
            ) | {"started_at": started_at.isoformat()}
            status, checkpoint = await self._execute_cycle(cycle_id, started_at, settings)
        except Exception as exc:
            with suppress(Exception):
                await self.repository.record_heartbeat(
                    "error",
                    heartbeat_details
                    | {
                        "completed_at": now_utc().isoformat(),
                        "checkpoint": "cycle_exception",
                        "error_type": type(exc).__name__,
                    },
                )
            raise
        await self.repository.record_heartbeat(
            status,
            heartbeat_details
            | {
                "completed_at": now_utc().isoformat(),
                "checkpoint": checkpoint,
            },
        )

    async def _execute_cycle(
        self,
        cycle_id: UUID,
        now: datetime,
        settings: BotSettings,
    ) -> tuple[str, str]:
        blocked_checkpoint: str | None = None
        if self.order_reconciliation_service is not None:
            await self.order_reconciliation_service.reconcile_live_orders()
        provider_health = await self.health_service.check()
        positions: list[Position] | None = None
        if self.portfolio_service is not None:
            positions = await self.portfolio_service.sync_positions(now)
        settings_errors = settings_validation_reasons(settings)
        if settings_errors:
            await self.repository.record_engine_event(
                "warning", "settings", "invalid_settings", {"reasons": settings_errors}
            )
            return "warning", "invalid_settings"
        live_reconciliation_pending = (
            settings.mode == "live" and await self._has_pending_live_reconciliation()
        )
        market_open = await self.market_data.is_market_open()
        symbols = await self.repository.load_enabled_watchlist()
        quotes = await self.market_data.get_quotes(symbols)
        try:
            strategy_version = await self.repository.load_active_strategy_version(
                required_status="active" if settings.mode == "live" else None,
            )
        except ValueError:
            await self._record_critical_event(
                "strategy",
                "strategy_version_row_invalid",
                {},
            )
            return "warning", "strategy_version_row_invalid"
        if strategy_version is None:
            await self.repository.record_engine_event(
                "warning", "strategy", "missing_strategy_version", {}
            )
            return "warning", "missing_strategy_version"
        account = await self._account_state_for_mode(settings.mode, now)
        for symbol in symbols:
            quote = quotes.get(symbol)
            risk_now = now
            risk_settings = settings
            risk_provider_health = provider_health
            risk_market_open = market_open
            risk_positions = positions
            risk_account = account
            risk_strategy_version = strategy_version
            if settings.mode == "live":
                risk_now = now_utc()
                risk_settings = await self.repository.load_bot_settings()
                if risk_settings.mode != "live":
                    await self._record_critical_event(
                        "live_execution",
                        "live_mode_changed_during_cycle",
                        {"symbol": symbol, "mode": risk_settings.mode},
                    )
                    blocked_checkpoint = "live_mode_changed_during_cycle"
                    break
                quote = (await self.market_data.get_quotes([symbol])).get(symbol)
            if quote is None:
                await self.repository.record_engine_event(
                    "warning", "market_data", "missing_quote", {"symbol": symbol}
                )
                continue
            features = await self._features_for_mode(settings.mode, symbol, quote)
            if settings.mode == "paper":
                risk_now = now_utc()
            live_feature_order_proposal_ready = True
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
                if features.raw.get("feature_source") == "provider_live_v1":
                    live_feature_order_proposal_ready = False
            if settings.mode == "live":
                risk_now = now_utc()
                risk_settings = await self.repository.load_bot_settings()
                if risk_settings.mode != "live":
                    await self._record_critical_event(
                        "live_execution",
                        "live_mode_changed_before_final_risk_evaluation",
                        {"symbol": symbol, "mode": risk_settings.mode},
                    )
                    blocked_checkpoint = "live_mode_changed_before_final_risk_evaluation"
                    break
                risk_provider_health = await self.health_service.check()
                risk_market_open = await self.market_data.is_market_open()
                risk_account = await self._account_state_for_mode("live", risk_now)
                if self.portfolio_service is not None:
                    risk_positions = await self.portfolio_service.sync_positions(risk_now)
                try:
                    refreshed_strategy = await self.repository.load_active_strategy_version(
                        required_status="active"
                    )
                except ValueError:
                    await self._record_critical_event(
                        "strategy",
                        "live_strategy_row_invalid_during_cycle",
                        {"symbol": symbol},
                    )
                    blocked_checkpoint = "live_strategy_row_invalid_during_cycle"
                    break
                if refreshed_strategy is None:
                    await self._record_critical_event(
                        "strategy",
                        "live_strategy_missing_during_cycle",
                        {"symbol": symbol},
                    )
                    blocked_checkpoint = "live_strategy_missing_during_cycle"
                    break
                if refreshed_strategy.id != strategy_version.id:
                    await self._record_critical_event(
                        "strategy",
                        "live_strategy_changed_during_cycle",
                        {"symbol": symbol},
                    )
                    blocked_checkpoint = "live_strategy_changed_during_cycle"
                    break
                risk_strategy_version = refreshed_strategy
            risk_evidence = cycle_risk_evidence(
                mode=risk_settings.mode,
                symbol=symbol,
                features=features,
                positions=risk_positions,
                account=risk_account,
            )
            features = with_risk_evidence(features, risk_evidence)
            signal = self.strategy.score(
                features,
                StrategyContext(
                    strategy_version_id=risk_strategy_version.id,
                    weights=risk_strategy_version.weights,
                    buy_threshold=risk_strategy_version.buy_threshold,
                    sell_threshold=risk_strategy_version.sell_threshold,
                    order_amount_krw=risk_settings.max_order_amount_krw,
                    sector=risk_evidence.sector or "unknown",
                ),
            )
            paper_idempotency_key = paper_signal_idempotency_key(
                signal, risk_now, risk_strategy_version.version
            )
            duplicate_order = (
                paper_idempotency_key is not None
                and await self.repository.idempotency_key_exists(paper_idempotency_key)
            )
            risk_input = RiskInput(
                settings=risk_settings,
                signal=signal,
                account_state=risk_account,
                quote=quote,
                now=risk_now,
                provider_health=risk_provider_health,
                market_open=risk_market_open,
                existing_position_pct=risk_evidence.existing_position_pct,
                sector_position_pct=risk_evidence.sector_position_pct,
                available_position_quantity=risk_evidence.available_position_quantity,
                critical_news_risk=risk_evidence.critical_news_risk,
                liquidity_ok=risk_evidence.liquidity_ok,
                volatility_ok=risk_evidence.volatility_ok,
                cooldown_active=(
                    await self.repository.has_recent_live_order_for_symbol(
                        symbol,
                        risk_now - LIVE_ORDER_COOLDOWN,
                    )
                    if risk_settings.mode == "live"
                    else False
                ),
                duplicate_order=duplicate_order,
                strategy_version_id=risk_strategy_version.id,
                strategy_status=risk_strategy_version.status,
                strategy_approved=(
                    risk_strategy_version.approved_by is not None
                    and risk_strategy_version.approved_at is not None
                ),
                shutdown_requested=self.shutdown_requested(),
            )
            match risk_settings.mode:
                case "paper":
                    risk_result = self.risk_service.evaluate_paper_order(risk_input)
                case "live":
                    risk_result = self.risk_service.evaluate_live_order(risk_input)
                case unreachable:
                    assert_never(unreachable)
            snapshot = DecisionSnapshot.create(
                cycle_id=cycle_id,
                signal=signal,
                strategy_version_id=risk_strategy_version.id,
                created_at=risk_now,
                feature_snapshot=feature_snapshot_from_signal(features, signal, quote),
                risk_snapshot=risk_result.to_dict(),
            )
            await self.repository.persist_decision_snapshot(snapshot)
            await self.repository.persist_feature_observations(snapshot)
            if not risk_settings.enabled:
                continue
            if paper_idempotency_key is None:
                continue
            match risk_settings.mode:
                case "paper":
                    await self.execution_service.create_paper_order(
                        snapshot, risk_result, paper_idempotency_key
                    )
                case "live":
                    if (
                        live_reconciliation_pending
                        or await self._has_pending_live_reconciliation()
                        or not live_feature_order_proposal_ready
                    ):
                        continue
                    order, _final_risk = await self.execution_service.propose_live_order(
                        snapshot,
                        risk_input,
                    )
                    if order.status not in {"blocked", "failed", "rejected"}:
                        blocked_checkpoint = "legacy_live_order_requires_manual_check"
                        break
                case unreachable:
                    assert_never(unreachable)
        if blocked_checkpoint is not None:
            return "warning", blocked_checkpoint
        return "ok", "cycle_completed"

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
            "live_pending_reconciliation_blocks_new_live_orders",
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
                return await self.feature_service.build_live_features(symbol, quote)
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
    quote: Quote,
) -> dict[str, object]:
    return {
        "technical_score": features.technical_score,
        "fundamental_score": features.fundamental_score,
        "market_sector_score": features.market_sector_score,
        "news_event_score": features.news_event_score,
        "portfolio_score": features.portfolio_score,
        "final_score": signal.final_score,
        "price_at_decision": quote.price_krw,
        "raw": features.raw,
    }


def cycle_risk_evidence(
    *,
    mode: str,
    symbol: str,
    features: FeatureVector,
    positions: list[Position] | None,
    account: AccountState | None,
) -> CycleRiskEvidence:
    sector = verified_feature_sector(features)
    if mode == "paper":
        critical_news_risk = bool_feature_evidence(features.raw, "critical_news_risk")
        critical_news_source = "feature_raw"
        if critical_news_risk is None:
            critical_news_risk = False
            critical_news_source = "simulated_paper_assumption"
        return CycleRiskEvidence(
            sector=sector,
            existing_position_pct=0.0,
            sector_position_pct=0.0,
            available_position_quantity=None,
            critical_news_risk=critical_news_risk,
            liquidity_ok=True,
            volatility_ok=True,
            source=(
                "paper_feature_evidence_with_simulated_exposure_assumptions"
                if critical_news_source == "feature_raw"
                else "simulated_paper_assumption"
            ),
            position_sync_verified=False,
            sector_exposure_verified=False,
        )

    position_pct = position_exposure_pct(symbol, positions, account)
    sector_pct = sector_exposure_pct(symbol, sector, positions, account)
    return CycleRiskEvidence(
        sector=sector,
        existing_position_pct=position_pct,
        sector_position_pct=sector_pct,
        available_position_quantity=position_quantity(symbol, positions),
        critical_news_risk=bool_feature_evidence(features.raw, "critical_news_risk"),
        liquidity_ok=bool_feature_evidence(features.raw, "liquidity_ok"),
        volatility_ok=bool_feature_evidence(features.raw, "volatility_ok"),
        source="provider_feature_and_portfolio_evidence",
        position_sync_verified=positions is not None and account_equity_is_valid(account),
        sector_exposure_verified=sector_pct is not None,
    )


def with_risk_evidence(
    features: FeatureVector,
    evidence: CycleRiskEvidence,
) -> FeatureVector:
    raw = dict(features.raw)
    raw["risk_evidence"] = {
        "source": evidence.source,
        "sector": evidence.sector,
        "existing_position_pct": evidence.existing_position_pct,
        "sector_position_pct": evidence.sector_position_pct,
        "available_position_quantity": evidence.available_position_quantity,
        "critical_news_risk": evidence.critical_news_risk,
        "liquidity_ok": evidence.liquidity_ok,
        "volatility_ok": evidence.volatility_ok,
        "position_sync_verified": evidence.position_sync_verified,
        "sector_exposure_verified": evidence.sector_exposure_verified,
    }
    return replace(features, raw=raw)


def verified_feature_sector(features: FeatureVector) -> str | None:
    evidence = features.raw.get("market_sector")
    if not isinstance(evidence, Mapping):
        return None
    source = features.raw.get("market_sector_source")
    if not isinstance(source, str) or source.strip().lower() in UNVERIFIED_SECTOR_SOURCES:
        return None
    evidence_symbol = evidence.get("symbol")
    if evidence_symbol != features.symbol:
        return None
    market = evidence.get("market")
    as_of = evidence.get("as_of")
    if not isinstance(market, str) or not market.strip():
        return None
    if not isinstance(as_of, str) or not as_of.strip():
        return None
    sector = evidence.get("sector")
    if not isinstance(sector, str) or sector.strip().lower() in UNKNOWN_SECTORS:
        return None
    return sector.strip()


def position_exposure_pct(
    symbol: str,
    positions: list[Position] | None,
    account: AccountState | None,
) -> float | None:
    if positions is None or not account_equity_is_valid(account):
        return None
    assert account is not None
    values = [position.market_value_krw for position in positions if position.symbol == symbol]
    if any(value < 0 for value in values):
        return None
    return sum(values) / account.equity_krw


def position_quantity(symbol: str, positions: list[Position] | None) -> int | None:
    if positions is None:
        return None
    quantities = [position.quantity for position in positions if position.symbol == symbol]
    if any(quantity < 0 for quantity in quantities):
        return None
    return sum(quantities)


def sector_exposure_pct(
    symbol: str,
    sector: str | None,
    positions: list[Position] | None,
    account: AccountState | None,
) -> float | None:
    if sector is None or positions is None or not account_equity_is_valid(account):
        return None
    assert account is not None
    sector_value = 0
    for position in positions:
        position_sector = sector if position.symbol == symbol else verified_position_sector(
            position.sector
        )
        if position_sector is None or position.market_value_krw < 0:
            return None
        if position_sector.casefold() == sector.casefold():
            sector_value += position.market_value_krw
    return sector_value / account.equity_krw


def verified_position_sector(sector: str) -> str | None:
    normalized = sector.strip()
    if normalized.lower() in UNKNOWN_SECTORS:
        return None
    return normalized


def account_equity_is_valid(account: AccountState | None) -> bool:
    return account is not None and account.synced and account.equity_krw > 0


def bool_feature_evidence(raw: Mapping[str, object], key: str) -> bool | None:
    value = raw.get(key)
    return value if isinstance(value, bool) else None


def _feature_unready_reasons(features: FeatureVector) -> list[str]:
    reasons = features.raw.get("feature_unready_reasons")
    if not isinstance(reasons, list):
        return ["feature_snapshot_not_live_ready"]
    return [reason for reason in reasons if isinstance(reason, str)]

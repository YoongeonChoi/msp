from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import BrokerOrderRequest, BrokerOrderResult
from app.application.ports.fundamentals_port import FundamentalsPort
from app.application.ports.news_port import NewsPort
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.risk_service import RiskService
from app.application.use_cases.run_trading_cycle import RunTradingCycle, kst_day_window
from app.domain.common.time import now_utc
from app.domain.fundamentals.entities import QuarterlyFundamentals
from app.domain.news_intel.entities import NewsClassification, NewsEvent
from app.domain.strategy.entities import FeatureVector, StrategyVersion
from app.domain.trading.entities import AccountState, BotSettings, Order, OrderStatus, Quote
from app.domain.trading.value_objects import StrategyWeights


class CountingBroker(TossMock):
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.place_order_calls = 0

    async def provider_health(self) -> bool:
        return self.healthy

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.place_order_calls += 1
        return await super().place_order(request)


class SuccessfulBroker(CountingBroker):
    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.place_order_calls += 1
        return BrokerOrderResult(
            provider_order_id="test-live-order-1",
            status="sent",
            raw_summary={
                "symbol": request.symbol,
                "side": request.side,
                "amount_krw": request.amount_krw,
                "quantity": request.quantity,
                "limit_price_krw": request.limit_price_krw,
                "idempotency_key_present": bool(request.idempotency_key),
            },
        )


class AccountStateBroker(SuccessfulBroker):
    def __init__(
        self,
        daily_order_count_verified: bool,
        daily_order_count: int = 0,
    ) -> None:
        super().__init__()
        self.daily_order_count_verified = daily_order_count_verified
        self.daily_order_count = daily_order_count

    async def get_account_state(self, now: datetime) -> AccountState:
        return AccountState(
            synced=True,
            cash_krw=5_000_000,
            equity_krw=12_000_000,
            daily_loss_pct=0.0,
            daily_order_count=self.daily_order_count,
            synced_at=now,
            daily_order_count_verified=self.daily_order_count_verified,
        )


class DailyCountFailingRepository(InMemoryRepository):
    async def count_system_live_orders_created_between(
        self,
        start: datetime,
        end: datetime,
    ) -> int:
        raise RuntimeError("count_unavailable")


class QuoteOverrideMarketData(KrxMock):
    def __init__(self, quotes: dict[str, Quote], market_open: bool | None = True) -> None:
        self.quotes = quotes
        self.market_open = market_open

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return self.quotes

    async def is_market_open(self) -> bool | None:
        return self.market_open


class PositiveFundamentals:
    async def provider_health(self) -> bool:
        return True

    async def get_latest(self, symbol: str) -> QuarterlyFundamentals:
        return QuarterlyFundamentals(
            symbol=symbol,
            per=11.0,
            pbr=1.0,
            roe=0.18,
            operating_margin=0.22,
            debt_ratio=0.30,
        )


class PositiveNews:
    async def provider_health(self) -> bool:
        return True

    async def get_recent(self, symbol: str) -> list[NewsEvent]:
        return [
            NewsEvent(
                symbol=symbol,
                title="Provider backed positive catalyst",
                source="naver",
                published_at=now_utc(),
                classification=NewsClassification(
                    symbol=symbol,
                    relevance_score=0.9,
                    sentiment="positive",
                    event_type="earnings",
                    risk_level="low",
                    summary_short="positive provider-backed fixture",
                    trading_relevance=0.9,
                    confidence=0.9,
                ),
            )
        ]


class LiveReadyFeatureService(FeatureService):
    async def build_live_features(self, symbol: str, quote: Quote) -> FeatureVector:
        liquidity = 0.8 if quote.price_krw > 0 else 0.0
        return FeatureVector(
            symbol=symbol,
            technical_score=0.82,
            fundamental_score=0.60,
            market_sector_score=0.60,
            news_event_score=0.65,
            portfolio_score=liquidity,
            raw={
                "quote_price_krw": quote.price_krw,
                "source": quote.source,
                "feature_source": "verified_test_fixture",
                "live_trading_ready": True,
            },
        )


class MockLiveFeatureService(FeatureService):
    async def build_live_features(self, symbol: str, quote: Quote) -> FeatureVector:
        liquidity = 0.8 if quote.price_krw > 0 else 0.0
        return FeatureVector(
            symbol=symbol,
            technical_score=0.82,
            fundamental_score=0.60,
            market_sector_score=0.60,
            news_event_score=0.65,
            portfolio_score=liquidity,
            raw={
                "quote_price_krw": quote.price_krw,
                "source": quote.source,
                "feature_source": "mock_static",
                "live_trading_ready": False,
            },
        )


async def test_bot_disabled_still_creates_signal_snapshot_without_orders() -> None:
    repository = InMemoryRepository(BotSettings(enabled=False))
    broker = CountingBroker()
    cycle = _cycle(repository, broker=broker)

    await cycle.execute()

    assert len(repository.heartbeats) == 1
    assert len(repository.api_health) == 5
    assert len(repository.decisions) == 1
    assert repository.decisions[0].signal.action == "buy"
    assert "bot_disabled" in repository.decisions[0].risk_snapshot["reasons"]
    assert len(repository.news_events) == 1
    assert len(repository.fundamentals_quarterly) == 1
    assert repository.orders == []
    assert broker.place_order_calls == 0


async def test_paper_enabled_creates_paper_order_only() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    cycle = _cycle(repository)

    await cycle.execute()

    assert len(repository.decisions) == 1
    assert len(repository.orders) == 1
    assert repository.orders[0].mode == "paper"
    assert repository.orders[0].status == "paper"
    assert repository.orders[0].idempotency_key
    assert repository.decisions[0].signal.reason_json
    assert repository.decisions[0].feature_snapshot
    assert repository.decisions[0].risk_snapshot
    assert "technical_score" in repository.decisions[0].feature_snapshot
    assert "fundamental_score" in repository.decisions[0].feature_snapshot
    assert "market_sector_score" in repository.decisions[0].feature_snapshot
    assert "news_event_score" in repository.decisions[0].feature_snapshot
    assert "portfolio_score" in repository.decisions[0].feature_snapshot
    assert "final_score" in repository.decisions[0].feature_snapshot


async def test_live_mode_is_blocked_when_live_permission_false() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=False)
    )
    broker = CountingBroker()
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    assert repository.orders[0].status == "blocked"
    assert repository.orders[0].reason is not None
    assert "live_order_allowed_false" in repository.orders[0].reason
    assert broker.place_order_calls == 0


async def test_live_mode_market_closed_keeps_signal_but_blocks_order_before_broker() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=True)
    quote = Quote(
        symbol="005930",
        price_krw=75_000,
        as_of=now_utc(),
        source="toss",
    )
    cycle = _cycle(
        repository,
        broker=broker,
        market_data=QuoteOverrideMarketData({"005930": quote}, market_open=False),
        feature_service=LiveReadyFeatureService(),
    )

    await cycle.execute()

    assert len(repository.decisions) == 1
    assert len(repository.orders) == 1
    assert repository.orders[0].status == "blocked"
    assert repository.orders[0].reason is not None
    assert "market_closed_or_unknown" in repository.orders[0].reason
    assert broker.place_order_calls == 0


async def test_paper_mode_never_creates_live_statuses_or_calls_broker() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    broker = CountingBroker()
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert {order.status for order in repository.orders} <= {"paper", "proposed", "blocked"}
    assert broker.place_order_calls == 0


async def test_duplicate_paper_signal_is_blocked() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    cycle = _cycle(repository)

    await cycle.execute()
    await cycle.execute()

    assert [order.status for order in repository.orders] == ["paper", "blocked"]
    assert repository.orders[1].reason is not None
    assert "duplicate_order_detected" in repository.orders[1].reason
    assert repository.engine_events[-1]["message"] == "paper_order_blocked"


async def test_stale_quote_blocks_paper_order() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    stale_quote = Quote(
        symbol="005930",
        price_krw=75_000,
        as_of=now_utc() - timedelta(seconds=120),
    )
    cycle = _cycle(repository, market_data=QuoteOverrideMarketData({"005930": stale_quote}))

    await cycle.execute()

    assert len(repository.orders) == 1
    assert repository.orders[0].status == "blocked"
    assert repository.orders[0].reason is not None
    assert "stale_quote" in repository.orders[0].reason


async def test_missing_strategy_blocks_paper_order() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    repository.strategy_version = None
    cycle = _cycle(repository)

    await cycle.execute()

    assert repository.decisions == []
    assert repository.orders == []
    assert repository.engine_events[-1]["message"] == "missing_strategy_version"


async def test_paper_cycle_uses_strategy_params_from_repository() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    assert repository.strategy_version is not None
    repository.strategy_version = StrategyVersion(
        id=repository.strategy_version.id,
        version="strategy_v1_weighted_factor",
        status="paper",
        strategy_type="WeightedFactorStrategyV1",
        weights=StrategyWeights(),
        buy_threshold=0.99,
        sell_threshold=0.01,
    )
    cycle = _cycle(repository)

    await cycle.execute()

    assert len(repository.decisions) == 1
    assert repository.decisions[0].signal.action == "hold"
    assert repository.orders == []


async def test_broker_provider_health_failure_does_not_stop_paper_signal_or_order() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    cycle = _cycle(repository, broker=CountingBroker(healthy=False))

    await cycle.execute()

    assert len(repository.decisions) == 1
    assert len(repository.orders) == 1
    assert repository.orders[0].status == "paper"
    assert any(
        item["provider"] == "toss" and item["healthy"] is False
        for item in repository.api_health
    )


def test_kst_day_window_uses_korean_trading_day_boundary() -> None:
    start, end = kst_day_window(datetime(2026, 6, 27, 15, 1, tzinfo=UTC))

    assert start.isoformat() == "2026-06-28T00:00:00+09:00"
    assert end.isoformat() == "2026-06-29T00:00:00+09:00"


async def test_live_allowed_cycle_blocks_without_verified_account_state() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = CountingBroker()
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    assert broker.place_order_calls == 0
    assert repository.orders[0].mode == "live"
    assert repository.orders[0].status == "blocked"
    assert repository.orders[0].reason is not None
    assert "missing_account_state" in repository.orders[0].reason
    assert any(
        event["message"] == "live_account_state_sync_failed"
        for event in repository.engine_events
    )
    assert repository.engine_events[-1]["message"] == "live_order_blocked_by_risk"


async def test_live_allowed_cycle_never_uses_simulated_account_for_broker_call() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = SuccessfulBroker()
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    assert broker.place_order_calls == 0
    order = repository.orders[0]
    assert order.mode == "live"
    assert order.status == "blocked"
    assert order.provider_order_id is None
    assert order.reason is not None
    assert "missing_account_state" in order.reason


async def test_live_allowed_cycle_uses_repository_count_when_broker_history_unverified() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=False)
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    order = repository.orders[0]
    assert broker.place_order_calls == 1
    assert order.mode == "live"
    assert order.status == "sent"
    assert order.provider_order_id == "test-live-order-1"
    assert not any(
        event["message"] == "live_system_order_count_sync_failed"
        for event in repository.engine_events
    )


async def test_live_allowed_cycle_blocks_without_system_order_scope_acceptance() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=False)
    cycle = _cycle(
        repository,
        broker=broker,
        feature_service=LiveReadyFeatureService(),
        live_system_order_count_scope_accepted=False,
    )

    await cycle.execute()

    assert len(repository.orders) == 1
    assert broker.place_order_calls == 0
    order = repository.orders[0]
    assert order.mode == "live"
    assert order.status == "blocked"
    assert order.reason is not None
    assert "daily_order_count_unverified" in order.reason
    assert any(
        event["message"] == "live_external_order_history_scope_not_accepted"
        for event in repository.engine_events
    )


async def test_live_allowed_cycle_blocks_when_system_daily_order_count_reaches_limit() -> None:
    repository = InMemoryRepository(
        BotSettings(
            enabled=True,
            mode="live",
            live_order_allowed=True,
            max_daily_order_count=1,
        )
    )
    repository.orders.append(_live_order(status="filled", created_at=now_utc()))
    broker = AccountStateBroker(
        daily_order_count_verified=False,
        daily_order_count=0,
    )
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 2
    assert broker.place_order_calls == 0
    order = repository.orders[-1]
    assert order.mode == "live"
    assert order.status == "blocked"
    assert order.reason is not None
    assert "max_daily_order_count_exceeded" in order.reason


async def test_live_allowed_cycle_blocks_when_system_daily_order_count_sync_fails() -> None:
    repository = DailyCountFailingRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=True)
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    assert broker.place_order_calls == 0
    order = repository.orders[0]
    assert order.mode == "live"
    assert order.status == "blocked"
    assert order.reason is not None
    assert "daily_order_count_unverified" in order.reason
    assert any(
        event["message"] == "live_system_order_count_sync_failed"
        for event in repository.engine_events
    )


async def test_live_cycle_blocks_new_orders_with_pending_live_reconciliation() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    repository.orders.append(
        _live_order(status="unknown_requires_manual_check", created_at=now_utc())
    )
    broker = AccountStateBroker(daily_order_count_verified=True)
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    assert len(repository.decisions) == 1
    assert broker.place_order_calls == 0
    assert any(
        event["message"] == "live_pending_reconciliation_blocks_new_live_orders"
        and event["level"] == "critical"
        and _event_details_value(event, "pending_order_count") == 1
        for event in repository.engine_events
    )


async def test_live_allowed_cycle_blocks_mock_strategy_features_before_broker() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=True)
    cycle = _cycle(repository, broker=broker, feature_service=MockLiveFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    assert broker.place_order_calls == 0
    order = repository.orders[0]
    assert order.mode == "live"
    assert order.status == "blocked"
    assert order.reason is not None
    assert "missing_live_decision_evidence" in order.reason
    assert "mock_strategy_features_not_live_ready" in order.reason
    assert any(
        event["message"] == "live_order_blocked_missing_evidence"
        and _event_reasons_include(event, "mock_strategy_features_not_live_ready")
        for event in repository.engine_events
    )


async def test_live_cycle_marks_default_mock_provider_features_not_ready() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=True)
    cycle = _cycle(repository, broker=broker)

    await cycle.execute()

    assert broker.place_order_calls == 0
    assert len(repository.decisions) == 1
    assert repository.orders == []
    raw = repository.decisions[0].feature_snapshot["raw"]
    assert isinstance(raw, dict)
    assert raw["feature_source"] == "provider_live_v1"
    assert raw["live_trading_ready"] is False
    assert any(
        event["message"] == "live_feature_snapshot_not_ready"
        and _event_reasons_include(event, "quote_source_not_live_provider")
        and _event_reasons_include(event, "fundamentals_source_not_live_provider")
        and _event_reasons_include(event, "news_provider_not_live_provider")
        for event in repository.engine_events
    )


async def test_live_cycle_blocks_provider_features_without_sector_evidence() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=True)
    quote = Quote(
        symbol="005930",
        price_krw=75_000,
        as_of=now_utc(),
        source="toss",
    )
    cycle = _cycle(
        repository,
        broker=broker,
        market_data=QuoteOverrideMarketData({"005930": quote}),
        fundamentals=PositiveFundamentals(),
        news=PositiveNews(),
    )

    await cycle.execute()

    assert repository.orders == []
    assert broker.place_order_calls == 0
    raw = repository.decisions[0].feature_snapshot["raw"]
    assert isinstance(raw, dict)
    assert raw["feature_source"] == "provider_live_v1"
    assert raw["live_trading_ready"] is False
    assert raw["quote_source"] == "toss"
    assert raw["fundamentals_source"] == "opendart"
    assert raw["news_provider"] == "naver"
    assert raw["news_sources"] == ["naver"]
    assert raw["feature_unready_reasons"] == ["market_sector_evidence_missing"]
    assert any(
        event["message"] == "live_feature_snapshot_not_ready"
        and _event_reasons_include(event, "market_sector_evidence_missing")
        for event in repository.engine_events
    )


async def test_live_allowed_cycle_places_order_with_verified_account_state() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = AccountStateBroker(daily_order_count_verified=True)
    cycle = _cycle(repository, broker=broker, feature_service=LiveReadyFeatureService())

    await cycle.execute()

    assert len(repository.orders) == 1
    order = repository.orders[0]
    assert broker.place_order_calls == 1
    assert order.mode == "live"
    assert order.status == "sent"
    assert order.provider_order_id == "test-live-order-1"


def _live_order(status: OrderStatus, created_at: datetime) -> Order:
    return Order(
        id=uuid4(),
        decision_id=uuid4(),
        symbol="005930",
        action="buy",
        mode="live",
        status=status,
        amount_krw=100_000,
        idempotency_key=f"existing-live-{uuid4()}",
        reason=None,
        created_at=created_at,
        provider_order_id="existing-provider-order",
    )


def _event_reasons_include(event: dict[str, object], reason: str) -> bool:
    details = event.get("details")
    if not isinstance(details, dict):
        return False
    reasons = details.get("reasons")
    return isinstance(reasons, list) and reason in reasons


def _event_details_value(event: dict[str, object], key: str) -> object:
    details = event.get("details")
    if not isinstance(details, dict):
        return None
    return details.get(key)


def _cycle(
    repository: InMemoryRepository,
    broker: CountingBroker | None = None,
    market_data: KrxMock | None = None,
    fundamentals: FundamentalsPort | None = None,
    news: NewsPort | None = None,
    feature_service: FeatureService | None = None,
    live_system_order_count_scope_accepted: bool = True,
) -> RunTradingCycle:
    risk = RiskService()
    safe_broker = broker or CountingBroker()
    safe_market_data = market_data or KrxMock()
    safe_fundamentals = fundamentals or OpenDartMock()
    safe_news = news or NaverNewsMock()
    fundamentals_provider_name = "opendart" if fundamentals is not None else "opendart_mock"
    news_provider_name = "naver" if news is not None else "naver_mock"
    return RunTradingCycle(
        repository=repository,
        broker=safe_broker,
        market_data=safe_market_data,
        health_service=HealthService(
            repository,
            safe_broker,
            safe_market_data,
            safe_fundamentals,
            safe_news,
            OpenAIMock(),
        ),
        execution_service=ExecutionService(safe_broker, repository, risk),
        risk_service=risk,
        feature_service=feature_service
        or FeatureService(
            fundamentals=safe_fundamentals,
            news=safe_news,
            fundamentals_provider_name=fundamentals_provider_name,
            news_provider_name=news_provider_name,
        ),
        live_system_order_count_scope_accepted=live_system_order_count_scope_accepted,
    )

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.application.ports.backtest_port import BacktestRows
from app.application.services.backtest_models import BacktestRequest
from app.application.services.backtest_service import BacktestService
from app.domain.common.json import JsonObject


async def test_backtest_runs_fixed_buy_sell_fixture() -> None:
    repository = FakeBacktestRepository(
        rows=BacktestRows(
            strategy=_strategy({"technical": 1.0}, {"buy_threshold": 0.7, "sell_threshold": 0.3}),
            features_daily=[
                _feature("005930", "2026-01-02", 100, technical_score=0.9),
                _feature("005930", "2026-01-05", 110, technical_score=0.1),
            ],
            fundamentals_quarterly=[],
            news_events=[],
            watchlist=[{"symbol": "005930", "sector": "semiconductor", "enabled": True}],
        )
    )

    result = await BacktestService(repository).run(
        BacktestRequest(
            strategy="strategy_v1_weighted_factor",
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
        )
    )

    assert result.number_of_trades == 2
    assert result.win_rate == 1.0
    assert result.total_return > 0.09
    assert repository.saved_results[-1]["strategy"] == "strategy_v1_weighted_factor"
    assert repository.live_orders_created == 0


async def test_backtest_applies_fee_and_slippage() -> None:
    repository = FakeBacktestRepository(
        rows=BacktestRows(
            strategy=_strategy(
                {"technical": 1.0},
                {
                    "buy_threshold": 0.7,
                    "sell_threshold": 0.3,
                    "fee_rate": 0.01,
                    "slippage_rate": 0.01,
                },
            ),
            features_daily=[
                _feature("005930", "2026-01-02", 100, technical_score=0.9),
                _feature("005930", "2026-01-05", 100, technical_score=0.1),
            ],
            fundamentals_quarterly=[],
            news_events=[],
            watchlist=[{"symbol": "005930", "sector": "semiconductor", "enabled": True}],
        )
    )

    result = await BacktestService(repository).run(_request())

    assert result.transaction_cost_krw > 0
    assert result.total_return < 0
    assert result.average_loss < 0


async def test_backtest_uses_watchlist_target_sell() -> None:
    repository = FakeBacktestRepository(
        rows=BacktestRows(
            strategy=_strategy({"technical": 1.0}, {"buy_threshold": 0.7, "sell_threshold": 0.3}),
            features_daily=[
                _feature("005930", "2026-01-02", 100, technical_score=0.9),
                _feature("005930", "2026-01-05", 105, technical_score=0.5),
            ],
            fundamentals_quarterly=[],
            news_events=[],
            watchlist=[
                {
                    "symbol": "005930",
                    "sector": "semiconductor",
                    "enabled": True,
                    "target_sell_krw": 104,
                }
            ],
        )
    )

    result = await BacktestService(repository).run(_request())

    assert result.number_of_trades == 2
    assert result.win_rate == 1.0
    assert result.total_return > 0.04


async def test_backtest_blocks_when_position_limit_exceeded() -> None:
    repository = FakeBacktestRepository(
        rows=BacktestRows(
            strategy=_strategy(
                {"technical": 1.0},
                {"buy_threshold": 0.7, "max_position_pct": 0.05, "max_order_amount_krw": 100_000},
            ),
            features_daily=[_feature("005930", "2026-01-02", 100, technical_score=0.9)],
            fundamentals_quarterly=[],
            news_events=[],
            watchlist=[{"symbol": "005930", "sector": "semiconductor", "enabled": True}],
        )
    )

    result = await BacktestService(repository).run(_request())

    assert result.number_of_trades == 0
    assert result.blocked_reason_counts["max_position_pct"] == 1
    assert repository.live_orders_created == 0


async def test_backtest_handles_missing_price_data_gracefully() -> None:
    repository = FakeBacktestRepository(
        rows=BacktestRows(
            strategy=_strategy({"technical": 1.0}, {"buy_threshold": 0.7}),
            features_daily=[
                {
                    "symbol": "005930",
                    "trade_date": "2026-01-02",
                    "technical_score": 0.9,
                }
            ],
            fundamentals_quarterly=[],
            news_events=[],
            watchlist=[{"symbol": "005930", "sector": "semiconductor", "enabled": True}],
        )
    )

    result = await BacktestService(repository).run(_request())

    assert result.number_of_trades == 0
    assert result.blocked_reason_counts["missing_price"] == 1
    assert repository.saved_results


@dataclass(slots=True)
class FakeBacktestRepository:
    rows: BacktestRows
    saved_results: list[JsonObject] = field(default_factory=list)
    live_orders_created: int = 0

    async def load_backtest_rows(
        self, strategy: str, start: date, end: date
    ) -> BacktestRows:
        return self.rows

    async def save_backtest_result(self, result: JsonObject) -> None:
        self.saved_results.append(result)


def _request() -> BacktestRequest:
    return BacktestRequest(
        strategy="strategy_v1_weighted_factor",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )


def _strategy(weights: JsonObject, params: JsonObject) -> JsonObject:
    return {
        "id": "strategy-id",
        "version": "strategy_v1_weighted_factor",
        "version_name": "strategy_v1_weighted_factor",
        "weights": weights,
        "params": {
            "initial_cash_krw": 1_000_000,
            "max_position_pct": 1.0,
            "max_sector_pct": 1.0,
            "max_daily_order_count": 10,
            "max_order_amount_krw": 1_000_000,
            "fee_rate": 0,
            "slippage_rate": 0,
            **params,
        },
    }


def _feature(
    symbol: str,
    trade_date: str,
    close_price: int,
    *,
    technical_score: float,
) -> JsonObject:
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "close_price": close_price,
        "technical_score": technical_score,
        "fundamental_score": 0.5,
        "market_sector_score": 0.5,
        "news_event_score": 0.5,
        "portfolio_score": 0.5,
    }

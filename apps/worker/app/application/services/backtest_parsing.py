from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from app.application.ports.backtest_port import BacktestRows
from app.application.services.backtest_models import BacktestAssumptions
from app.domain.common.json import JsonObject, JsonValue
from app.domain.trading.value_objects import StrategyWeights


@dataclass(frozen=True, slots=True)
class BacktestStrategy:
    version: str
    weights: StrategyWeights
    buy_threshold: float
    sell_threshold: float
    assumptions: BacktestAssumptions


@dataclass(frozen=True, slots=True)
class DailyFeature:
    symbol: str
    trade_date: date
    close_price: float | None
    sector: str
    target_sell_krw: float | None
    stop_loss_pct: float | None
    technical_score: float
    fundamental_score: float
    market_sector_score: float
    news_event_score: float
    portfolio_score: float


@dataclass(frozen=True, slots=True)
class WatchlistConfig:
    sector: str
    target_sell_krw: float | None
    stop_loss_pct: float | None


@dataclass(frozen=True, slots=True)
class ParsedBacktestRows:
    strategy: BacktestStrategy | None
    features_by_date: dict[date, list[DailyFeature]]


def parse_backtest_rows(rows: BacktestRows, strategy_name: str) -> ParsedBacktestRows:
    watchlist = _watchlist_by_symbol(rows.watchlist)
    news_scores = _news_scores(rows.news_events)
    fundamental_scores = _fundamental_scores(rows.fundamentals_quarterly)
    features_by_date: dict[date, list[DailyFeature]] = defaultdict(list)
    for row in rows.features_daily:
        feature = _parse_feature(row, watchlist, news_scores, fundamental_scores)
        if feature is not None:
            features_by_date[feature.trade_date].append(feature)
    return ParsedBacktestRows(
        strategy=_parse_strategy(rows.strategy, strategy_name),
        features_by_date={
            key: sorted(value, key=lambda item: item.symbol)
            for key, value in features_by_date.items()
        },
    )


def _parse_strategy(row: JsonObject | None, fallback_version: str) -> BacktestStrategy | None:
    if row is None:
        return None
    weights = _json_object(row.get("weights_json")) or _json_object(row.get("weights"))
    params = _json_object(row.get("params_json")) or _json_object(row.get("params"))
    return BacktestStrategy(
        version=_string(row.get("version")) or _string(row.get("version_name")) or fallback_version,
        weights=StrategyWeights(
            technical=_number_or(weights, "technical", 0.35),
            fundamental=_number_or(weights, "fundamental", 0.25),
            market_sector=_number_or(weights, "market_sector", 0.15),
            news_event=_number_or(weights, "news_event", 0.15),
            portfolio=_number_or(weights, "portfolio", 0.10),
        ),
        buy_threshold=_number_or(params, "buy_threshold", 0.68),
        sell_threshold=_number_or(params, "sell_threshold", 0.25),
        assumptions=BacktestAssumptions(
            initial_cash_krw=_number_or(params, "initial_cash_krw", 1_000_000.0),
            transaction_fee_rate=_number_or(params, "fee_rate", 0.00015),
            slippage_rate=_number_or(params, "slippage_rate", 0.0005),
            max_position_pct=_number_or(params, "max_position_pct", 0.10),
            max_sector_pct=_number_or(params, "max_sector_pct", 0.30),
            max_daily_order_count=_int_or(params, "max_daily_order_count", 10),
            max_order_amount_krw=_number_or(params, "max_order_amount_krw", 100_000.0),
            target_return_pct=_optional_number(params, "target_return_pct"),
            stop_loss_pct=_optional_number(params, "stop_loss_pct"),
        ),
    )


def _parse_feature(
    row: JsonObject,
    watchlist: dict[str, WatchlistConfig],
    news_scores: dict[str, float],
    fundamental_scores: dict[str, float],
) -> DailyFeature | None:
    trade_date = _date(row.get("trade_date"))
    symbol = _string(row.get("symbol"))
    if trade_date is None or not symbol:
        return None
    raw = _json_object(row.get("raw_snapshot"))
    watch = watchlist.get(symbol)
    return DailyFeature(
        symbol=symbol,
        trade_date=trade_date,
        close_price=_number_from(
            row,
            raw,
            ("close_price", "close", "price_krw", "current_price_krw"),
        ),
        sector=_string(row.get("sector")) or (watch.sector if watch else "unknown"),
        target_sell_krw=_optional_number(row, "target_sell_krw")
        or (watch.target_sell_krw if watch else None),
        stop_loss_pct=_optional_number(row, "stop_loss_pct")
        or (watch.stop_loss_pct if watch else None),
        technical_score=_score(row, raw, "technical_score", _technical_score(row)),
        fundamental_score=_score(
            row,
            raw,
            "fundamental_score",
            fundamental_scores.get(symbol, 0.5),
        ),
        market_sector_score=_score(row, raw, "market_sector_score", 0.5),
        news_event_score=_score(row, raw, "news_event_score", news_scores.get(symbol, 0.5)),
        portfolio_score=_score(row, raw, "portfolio_score", 0.5),
    )


def _watchlist_by_symbol(rows: list[JsonObject]) -> dict[str, WatchlistConfig]:
    return {
        symbol: WatchlistConfig(
            sector=_string(row.get("sector")) or "unknown",
            target_sell_krw=_optional_number(row, "target_sell_krw"),
            stop_loss_pct=_optional_number(row, "stop_loss_pct"),
        )
        for row in rows
        if (symbol := _string(row.get("symbol")))
    }


def _news_scores(rows: list[JsonObject]) -> dict[str, float]:
    scores: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        symbol = _string(row.get("symbol"))
        if symbol:
            scores[symbol].append(_news_score(row))
    return {symbol: sum(values) / len(values) for symbol, values in scores.items() if values}


def _fundamental_scores(rows: list[JsonObject]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for row in rows:
        symbol = _string(row.get("symbol"))
        if symbol:
            scores[symbol] = _fundamental_score(row)
    return scores


def _technical_score(row: JsonObject) -> float:
    r_5d = _optional_number(row, "r_5d") or 0.0
    r_20d = _optional_number(row, "r_20d") or 0.0
    liquidity = 1.0 if row.get("liquidity_filter") is True else 0.5
    return _clamp(0.5 + r_5d + (r_20d * 0.5) + ((liquidity - 0.5) * 0.2))


def _fundamental_score(row: JsonObject) -> float:
    roe = _optional_number(row, "roe") or 0.0
    margin = _optional_number(row, "operating_margin") or 0.0
    debt_ratio = _optional_number(row, "debt_ratio") or 1.0
    return _clamp(0.5 + (roe * 0.5) + (margin * 0.3) - (max(0.0, debt_ratio - 1.0) * 0.1))


def _news_score(row: JsonObject) -> float:
    risk_level = _string(row.get("risk_level"))
    sentiment = _string(row.get("sentiment"))
    if risk_level == "critical":
        return 0.0
    if sentiment == "positive":
        return 0.75
    if sentiment == "negative":
        return 0.25
    return 0.5


def _score(row: JsonObject, raw: JsonObject, key: str, default: float) -> float:
    value = _optional_number(row, key)
    if value is None:
        value = _optional_number(raw, key)
    return _clamp(default if value is None else value)


def _number_from(row: JsonObject, raw: JsonObject, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _optional_number(row, key)
        if value is not None:
            return value
        raw_value = _optional_number(raw, key)
        if raw_value is not None:
            return raw_value
    return None


def _optional_number(row: JsonObject, key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _number_or(row: JsonObject, key: str, default: float) -> float:
    return _optional_number(row, key) or default


def _int_or(row: JsonObject, key: str, default: int) -> int:
    value = _optional_number(row, key)
    return default if value is None else int(value)


def _string(value: JsonValue | None) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _json_object(value: JsonValue | None) -> JsonObject:
    if isinstance(value, dict):
        return value
    return {}


def _date(value: JsonValue | None) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from app.application.ports.candle_data_port import (
    DailyCandleReadPage,
    DailyCandleReadRequest,
    DailyCandleSourcePort,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.time import now_utc
from app.domain.market_data.point_in_time import (
    PointInTimeDataError,
    validate_point_in_time_candle_page,
)

_KR_SYMBOL_RE = re.compile(r"[0-9]{6}")


class CandleCollectionError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("candle_collection", safe_message)


class DataCollectionService:
    def __init__(
        self,
        source: DailyCandleSourcePort,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.source = source
        self.clock = clock

    async def collect_daily_candle_page(
        self,
        request: DailyCandleReadRequest,
    ) -> DailyCandleReadPage:
        started_at = _read_clock(self.clock)
        _validate_request(request, started_at=started_at)
        page = await self.source.read_daily_candle_page(request)
        completed_at = _read_clock(self.clock)
        if completed_at < started_at:
            raise CandleCollectionError("candle_collection_clock_moved_backwards")
        _validate_page(
            page,
            request=request,
            started_at=started_at,
            completed_at=completed_at,
        )
        return page


def _validate_request(
    request: object,
    *,
    started_at: datetime,
) -> None:
    if not isinstance(request, DailyCandleReadRequest):
        raise CandleCollectionError("candle_collection_request_invalid")
    if (
        not isinstance(request.symbol, str)
        or _KR_SYMBOL_RE.fullmatch(request.symbol) is None
    ):
        raise CandleCollectionError("candle_collection_symbol_invalid")
    if (
        not isinstance(request.count, int)
        or isinstance(request.count, bool)
        or not 1 <= request.count <= 200
    ):
        raise CandleCollectionError("candle_collection_count_out_of_range")
    if not isinstance(request.adjusted, bool):
        raise CandleCollectionError("candle_collection_adjusted_must_be_boolean")
    before = _require_aware_timestamp(
        request.before,
        "candle_collection_before_timezone_missing",
    )
    if before > started_at:
        raise CandleCollectionError("candle_collection_before_is_in_future")


def _validate_page(
    page: object,
    *,
    request: DailyCandleReadRequest,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    if not isinstance(page, DailyCandleReadPage):
        raise CandleCollectionError("candle_collection_page_invalid")
    if not isinstance(page.candles, tuple):
        raise CandleCollectionError("candle_collection_page_candles_invalid")
    observed_at = _require_aware_timestamp(
        page.observed_at,
        "candle_collection_observed_at_timezone_missing",
    )
    if observed_at < started_at or observed_at > completed_at:
        raise CandleCollectionError("candle_collection_observed_at_outside_read")
    try:
        candles = validate_point_in_time_candle_page(page.candles)
    except PointInTimeDataError as exc:
        raise CandleCollectionError(exc.safe_message) from exc
    if len(candles) > request.count:
        raise CandleCollectionError("candle_collection_page_exceeds_count")
    if any(candle.symbol != request.symbol for candle in candles):
        raise CandleCollectionError("candle_collection_symbol_mismatch")
    if any(candle.adjusted is not request.adjusted for candle in candles):
        raise CandleCollectionError("candle_collection_adjusted_mismatch")
    if any(candle.provider_event_at > request.before for candle in candles):
        raise CandleCollectionError("candle_collection_candle_after_before")
    if any(candle.observed_at != observed_at for candle in candles):
        raise CandleCollectionError("candle_collection_observation_time_mismatch")
    providers = {candle.provider for candle in candles}
    if len(providers) > 1:
        raise CandleCollectionError("candle_collection_mixed_providers")
    contract_hashes = {candle.provider_contract_sha256 for candle in candles}
    if len(contract_hashes) > 1:
        raise CandleCollectionError("candle_collection_mixed_contract_hashes")
    next_before = page.next_before
    if next_before is None:
        return
    next_before = _require_aware_timestamp(
        next_before,
        "candle_collection_next_before_timezone_missing",
    )
    if next_before >= request.before:
        raise CandleCollectionError("candle_collection_next_before_not_progressing")
    if candles and next_before > min(c.provider_event_at for c in candles):
        raise CandleCollectionError("candle_collection_next_before_after_oldest")


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    return _require_aware_timestamp(
        clock(),
        "candle_collection_clock_timezone_missing",
    )


def _require_aware_timestamp(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CandleCollectionError(reason)
    return value


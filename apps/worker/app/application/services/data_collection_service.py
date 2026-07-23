from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from app.application.ports.candle_data_port import (
    DailyCandleReadPage,
    DailyCandleReadRequest,
    DailyCandleSourcePort,
)
from app.domain.common.errors import (
    KnownFailClosedError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
)
from app.domain.common.time import now_utc
from app.domain.market_data.point_in_time import (
    PointInTimeCandleV1,
    PointInTimeDataError,
    validate_point_in_time_candle_page,
)

_KR_SYMBOL_RE = re.compile(r"[0-9]{6}")

CandleCollectionReadOutcome = Literal[
    "not_attempted",
    "failed",
    "unknown",
    "succeeded",
]


class CandleCollectionError(KnownFailClosedError):
    def __init__(
        self,
        safe_message: str,
        *,
        read_outcome: CandleCollectionReadOutcome = "not_attempted",
    ) -> None:
        super().__init__("candle_collection", safe_message)
        self.read_outcome = read_outcome


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
        canonical_request = _canonical_request(request, started_at=started_at)
        page: object = None
        source_failure: CandleCollectionReadOutcome | None = None
        try:
            page = await self.source.read_daily_candle_page(canonical_request)
        except (ProviderAuthError, ProviderRateLimitError):
            source_failure = "failed"
        except ProviderSchemaError:
            source_failure = "succeeded"
        except ProviderError:
            source_failure = "unknown"
        except Exception:
            source_failure = "unknown"
        if source_failure is not None:
            raise CandleCollectionError(
                "candle_collection_source_failed",
                read_outcome=source_failure,
            )
        try:
            completed_at = _read_clock(self.clock)
            if completed_at < started_at:
                raise CandleCollectionError("candle_collection_clock_moved_backwards")
            return _canonical_page(
                page,
                request=canonical_request,
                started_at=started_at,
                completed_at=completed_at,
            )
        except CandleCollectionError as exc:
            raise CandleCollectionError(
                exc.safe_message,
                read_outcome="succeeded",
            ) from None


def _canonical_request(
    request: object,
    *,
    started_at: datetime,
) -> DailyCandleReadRequest:
    if type(request) is not DailyCandleReadRequest:
        raise CandleCollectionError("candle_collection_request_invalid")
    if type(request.symbol) is not str or _KR_SYMBOL_RE.fullmatch(request.symbol) is None:
        raise CandleCollectionError("candle_collection_symbol_invalid")
    if type(request.count) is not int or not 1 <= request.count <= 200:
        raise CandleCollectionError("candle_collection_count_out_of_range")
    if type(request.adjusted) is not bool:
        raise CandleCollectionError("candle_collection_adjusted_must_be_boolean")
    before = _require_aware_timestamp(
        request.before,
        "candle_collection_before_timezone_missing",
    )
    if before > started_at:
        raise CandleCollectionError("candle_collection_before_is_in_future")
    return DailyCandleReadRequest(
        symbol=request.symbol,
        before=before,
        count=request.count,
        adjusted=request.adjusted,
    )


def _canonical_page(
    page: object,
    *,
    request: DailyCandleReadRequest,
    started_at: datetime,
    completed_at: datetime,
) -> DailyCandleReadPage:
    if type(page) is not DailyCandleReadPage:
        raise CandleCollectionError("candle_collection_page_invalid")
    if type(page.candles) is not tuple:
        raise CandleCollectionError("candle_collection_page_candles_invalid")
    observed_at = _require_aware_timestamp(
        page.observed_at,
        "candle_collection_observed_at_timezone_missing",
    )
    if observed_at < started_at or observed_at > completed_at:
        raise CandleCollectionError("candle_collection_observed_at_outside_read")
    candles: tuple[PointInTimeCandleV1, ...] = ()
    candle_error_reason: str | None = None
    try:
        candles = validate_point_in_time_candle_page(page.candles)
    except PointInTimeDataError as exc:
        candle_error_reason = exc.safe_message
    if candle_error_reason is not None:
        raise CandleCollectionError(candle_error_reason)
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
        canonical_next_before = None
    else:
        canonical_next_before = _require_aware_timestamp(
            next_before,
            "candle_collection_next_before_timezone_missing",
        )
        if canonical_next_before >= request.before:
            raise CandleCollectionError("candle_collection_next_before_not_progressing")
        if candles and canonical_next_before > min(c.provider_event_at for c in candles):
            raise CandleCollectionError("candle_collection_next_before_after_oldest")
    return DailyCandleReadPage(
        candles=candles,
        next_before=canonical_next_before,
        observed_at=observed_at,
    )


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    value: object = None
    failed = False
    try:
        value = clock()
    except Exception:
        failed = True
    if failed:
        raise CandleCollectionError("candle_collection_clock_invalid")
    return _require_aware_timestamp(
        value,
        "candle_collection_clock_timezone_missing",
    )


def _require_aware_timestamp(value: object, reason: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise CandleCollectionError(reason)
    return value.astimezone(UTC)

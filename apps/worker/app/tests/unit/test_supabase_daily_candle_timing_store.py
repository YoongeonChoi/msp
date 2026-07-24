from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_daily_candle_timing_store import (
    PIT_DAILY_CANDLE_TIMING_RPC_ALLOWLIST,
    SupabaseDailyCandleTimingStore,
)
from app.application.ports.daily_candle_timing_store_port import (
    DailyCandleTimingStoreError,
)
from app.config import Settings
from app.domain.market_data.daily_candle_timing import (
    PointInTimeDailyCandleTimingEvidenceV1,
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)
from app.tests.unit.test_daily_candle_timing import (
    CUTOFF,
    REGULAR_END,
    _candle,
    _session,
)

REQUEST_KEY = "c" * 64
QUARANTINE_ID = "a7eeb600-d8d3-4e8b-a9b7-6330666c0590"
QUARANTINE_REASONS = (
    "pit_calendar_observation_time_regressed",
    "pit_calendar_historical_hash_recurrence_ambiguous",
    "pit_calendar_revision_time_not_increasing",
    "pit_timing_request_idempotency_conflict",
    "pit_timing_candle_revision_missing",
    "pit_timing_calendar_revision_missing",
    "pit_timing_source_binding_mismatch",
    "pit_timing_available_at_mismatch",
    "pit_timing_canonical_sha256_mismatch",
    "pit_timing_observation_time_regressed",
    "pit_timing_historical_hash_recurrence_ambiguous",
    "pit_timing_revision_time_not_increasing",
)


async def test_store_posts_canonical_binding_to_only_worker_rpc() -> None:
    calendar, timing = _bound_evidence()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["content-profile"] == "worker_api"
        assert request.url.path.endswith(
            "/append_pit_daily_candle_timing_evidence_v1"
        )
        assert payload == {
            "p_request_idempotency_key": REQUEST_KEY,
            "p_calendar": calendar.to_payload(),
            "p_timing": timing.to_payload(),
        }
        return httpx.Response(200, json=[_receipt(timing)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        receipt = await store.append_timing_evidence(
            REQUEST_KEY,
            calendar,
            timing,
        )
    finally:
        await client.aclose()

    assert receipt.status == "stored"
    assert receipt.request_idempotency_key == REQUEST_KEY
    assert receipt.timing_idempotency_key == timing.idempotency_key
    assert receipt.canonical_timing_evidence_sha256 == (
        timing.canonical_timing_evidence_sha256
    )
    assert receipt.calendar_revision == 1
    assert receipt.timing_revision == 1
    assert receipt.calendar_inserted is True
    assert receipt.timing_inserted is True
    assert receipt.evidence_available_at == timing.evidence_available_at
    assert receipt.quarantine_id is None
    assert receipt.reason_code is None
    assert len(requests) == 1
    assert {
        "append_pit_daily_candle_timing_evidence_v1"
    } == PIT_DAILY_CANDLE_TIMING_RPC_ALLOWLIST


async def test_store_accepts_strict_replay_receipt() -> None:
    calendar, timing = _bound_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _receipt(
                    timing,
                    status="replayed",
                    calendar_inserted=False,
                    timing_inserted=False,
                )
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        receipt = await store.append_timing_evidence(
            REQUEST_KEY,
            calendar,
            timing,
        )
    finally:
        await client.aclose()

    assert receipt.status == "replayed"
    assert receipt.calendar_inserted is False
    assert receipt.timing_inserted is False


@pytest.mark.parametrize("reason_code", QUARANTINE_REASONS)
async def test_store_turns_allowed_quarantine_receipt_into_fail_closed_error(
    reason_code: str,
) -> None:
    calendar, timing = _bound_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_quarantine_receipt(reason_code=reason_code)],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        with pytest.raises(DailyCandleTimingStoreError, match=reason_code):
            await store.append_timing_evidence(
                REQUEST_KEY,
                calendar,
                timing,
            )
    finally:
        await client.aclose()


async def test_store_accepts_matching_optional_quarantine_context() -> None:
    calendar, timing = _bound_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        row = _quarantine_receipt(
            reason_code="pit_timing_observation_time_regressed"
        )
        row.update(
            {
                "timing_idempotency_key": timing.idempotency_key,
                "canonical_timing_evidence_sha256": (
                    timing.canonical_timing_evidence_sha256
                ),
                "calendar_revision": 2,
                "timing_revision": 3,
                "evidence_available_at": (
                    timing.evidence_available_at.isoformat()
                ),
            }
        )
        return httpx.Response(200, json=[row])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        with pytest.raises(
            DailyCandleTimingStoreError,
            match="pit_timing_observation_time_regressed",
        ):
            await store.append_timing_evidence(
                REQUEST_KEY,
                calendar,
                timing,
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"status": "accepted"},
        {"request_idempotency_key": "d" * 64},
        {"timing_idempotency_key": "d" * 64},
        {"canonical_timing_evidence_sha256": "d" * 64},
        {"calendar_revision": True},
        {"timing_revision": 0},
        {"calendar_inserted": 1},
        {"timing_inserted": False},
        {"evidence_available_at": "not-a-time"},
        {"quarantine_id": QUARANTINE_ID},
    ],
)
async def test_store_rejects_malformed_or_mismatched_accepted_receipt(
    mutation: dict[str, object],
) -> None:
    calendar, timing = _bound_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        row = _receipt(timing)
        row.update(mutation)
        return httpx.Response(200, json=[row])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        with pytest.raises(DailyCandleTimingStoreError):
            await store.append_timing_evidence(
                REQUEST_KEY,
                calendar,
                timing,
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mutation",
    [
        {"quarantine_id": "not-a-uuid"},
        {"reason_code": "unapproved_reason"},
        {"timing_idempotency_key": "d" * 64},
        {"canonical_timing_evidence_sha256": "d" * 64},
        {"calendar_revision": 0},
        {"timing_revision": True},
        {"timing_inserted": True},
        {"evidence_available_at": "not-a-time"},
    ],
)
async def test_store_rejects_invalid_quarantine_context(
    mutation: dict[str, object],
) -> None:
    calendar, timing = _bound_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        row = _quarantine_receipt(
            reason_code="pit_timing_source_binding_mismatch"
        )
        row.update(mutation)
        return httpx.Response(200, json=[row])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        with pytest.raises(
            DailyCandleTimingStoreError,
            match="quarantine_receipt_invalid|rpc_.*_invalid",
        ):
            await store.append_timing_evidence(
                REQUEST_KEY,
                calendar,
                timing,
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize("body", [[], [{}, {}], {}, [None]])
async def test_store_requires_exact_singleton_rpc_row(body: object) -> None:
    calendar, timing = _bound_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        with pytest.raises(
            DailyCandleTimingStoreError,
            match="daily_candle_timing_store_rpc_result_invalid",
        ):
            await store.append_timing_evidence(
                REQUEST_KEY,
                calendar,
                timing,
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, content=b"not-json"),
    ],
)
async def test_store_maps_transport_and_json_failures_to_known_error(
    response: httpx.Response,
) -> None:
    calendar, timing = _bound_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        with pytest.raises(
            DailyCandleTimingStoreError,
            match="rpc_failed_or_returned_invalid_json",
        ):
            await store.append_timing_evidence(
                REQUEST_KEY,
                calendar,
                timing,
            )
    finally:
        await client.aclose()


async def test_store_revalidates_inputs_before_network() -> None:
    requests = 0
    calendar, timing = _bound_evidence()
    tampered_calendar = _session()
    object.__setattr__(tampered_calendar, "provider_contract_sha256", "d" * 64)
    tampered_timing = build_daily_candle_timing_evidence(_candle(), _session())
    object.__setattr__(
        tampered_timing,
        "calendar_observed_at",
        CUTOFF + timedelta(hours=1),
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        invalid_calls = (
            ("not-a-sha", calendar, timing),
            (REQUEST_KEY, cast(Any, object()), timing),
            (REQUEST_KEY, tampered_calendar, timing),
            (REQUEST_KEY, calendar, cast(Any, object())),
            (REQUEST_KEY, calendar, tampered_timing),
        )
        for request_key, invalid_calendar, invalid_timing in invalid_calls:
            with pytest.raises(DailyCandleTimingStoreError):
                await store.append_timing_evidence(
                    request_key,
                    invalid_calendar,
                    invalid_timing,
                )
    finally:
        await client.aclose()

    assert requests == 0


@pytest.mark.parametrize(
    "mismatched_calendar",
    [
        _session(provider="other"),
        _session(regular_end_at=REGULAR_END - timedelta(minutes=1)),
        _session(observed_at=CUTOFF + timedelta(minutes=1)),
    ],
)
async def test_store_rejects_valid_but_mismatched_calendar_binding_before_network(
    mismatched_calendar: PointInTimeKrDailySessionV1,
) -> None:
    timing = build_daily_candle_timing_evidence(
        _candle(provider=mismatched_calendar.provider),
        mismatched_calendar,
    )
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleTimingStore(_settings(), client=client)
    try:
        with pytest.raises(
            DailyCandleTimingStoreError,
            match="daily_candle_timing_store_calendar_binding_mismatch",
        ):
            await store.append_timing_evidence(
                REQUEST_KEY,
                _session(),
                timing,
            )
    finally:
        await client.aclose()

    assert requests == 0


def test_store_requires_worker_credentials() -> None:
    with pytest.raises(
        DailyCandleTimingStoreError,
        match="daily_candle_timing_store_credentials_missing",
    ):
        SupabaseDailyCandleTimingStore(Settings())


def _bound_evidence() -> tuple[
    PointInTimeKrDailySessionV1,
    PointInTimeDailyCandleTimingEvidenceV1,
]:
    calendar = _session()
    return calendar, build_daily_candle_timing_evidence(_candle(), calendar)


def _receipt(
    timing: PointInTimeDailyCandleTimingEvidenceV1,
    *,
    status: str = "stored",
    calendar_inserted: bool = True,
    timing_inserted: bool = True,
) -> dict[str, object]:
    return {
        "status": status,
        "request_idempotency_key": REQUEST_KEY,
        "timing_idempotency_key": timing.idempotency_key,
        "canonical_timing_evidence_sha256": (
            timing.canonical_timing_evidence_sha256
        ),
        "calendar_revision": 1,
        "timing_revision": 1,
        "calendar_inserted": calendar_inserted,
        "timing_inserted": timing_inserted,
        "evidence_available_at": timing.evidence_available_at.isoformat(),
        "quarantine_id": None,
        "reason_code": None,
    }


def _quarantine_receipt(*, reason_code: str) -> dict[str, object]:
    return {
        "status": "quarantined",
        "request_idempotency_key": REQUEST_KEY,
        "timing_idempotency_key": None,
        "canonical_timing_evidence_sha256": None,
        "calendar_revision": None,
        "timing_revision": None,
        "calendar_inserted": False,
        "timing_inserted": False,
        "evidence_available_at": None,
        "quarantine_id": QUARANTINE_ID,
        "reason_code": reason_code,
    }


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "SUPABASE_URL": "http://127.0.0.1:54321",
            "SUPABASE_SECRET_KEY": SecretStr("test-secret"),
        }
    )

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_candle_observation_store import (
    PIT_CANDLE_RPC_ALLOWLIST,
    SupabaseCandleObservationStore,
)
from app.application.ports.candle_observation_store_port import (
    CandleObservationStoreError,
)
from app.config import Settings
from app.tests.unit.test_candle_observation_store import (
    OBSERVED_AT,
    _candle,
)

QUARANTINE_ID = "a7eeb600-d8d3-4e8b-a9b7-6330666c0590"


async def test_store_posts_canonical_candle_to_only_worker_rpc() -> None:
    candle = _candle()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["content-profile"] == "worker_api"
        assert request.url.path.endswith("/append_pit_candle_observation_v1")
        assert payload == {"p_candle": candle.to_payload()}
        return httpx.Response(200, json=[_receipt(candle, inserted=True)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    try:
        receipt = await store.append_observation(candle)
    finally:
        await client.aclose()

    assert receipt.idempotency_key == candle.idempotency_key
    assert receipt.canonical_observation_sha256 == (
        candle.canonical_observation_sha256
    )
    assert receipt.revision == 1
    assert receipt.inserted is True
    assert receipt.stored_observed_at == candle.observed_at
    assert len(requests) == 1
    assert {"append_pit_candle_observation_v1"} == PIT_CANDLE_RPC_ALLOWLIST


async def test_store_accepts_strict_latest_replay_receipt() -> None:
    candle = _candle(observed_at=OBSERVED_AT + timedelta(minutes=5))
    stored_at = OBSERVED_AT.isoformat()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _receipt(
                    candle,
                    inserted=False,
                    status="replayed",
                    stored_at=stored_at,
                )
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    try:
        receipt = await store.append_observation(candle)
    finally:
        await client.aclose()

    assert receipt.inserted is False
    assert receipt.stored_observed_at == OBSERVED_AT


async def test_store_turns_committed_quarantine_receipt_into_fail_closed_error() -> None:
    candle = _candle()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _receipt(
                    candle,
                    inserted=False,
                    status="quarantined",
                    quarantine_id=QUARANTINE_ID,
                    reason_code=(
                        "candle_observation_store_revision_time_not_increasing"
                    ),
                )
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    try:
        with pytest.raises(
            CandleObservationStoreError,
            match="candle_observation_store_revision_time_not_increasing",
        ):
            await store.append_observation(candle)
    finally:
        await client.aclose()


async def test_store_preserves_regression_quarantine_reason_with_later_head() -> None:
    candle = _candle(observed_at=OBSERVED_AT - timedelta(minutes=5))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _receipt(
                    candle,
                    inserted=False,
                    status="quarantined",
                    stored_at=OBSERVED_AT.isoformat(),
                    quarantine_id=QUARANTINE_ID,
                    reason_code=(
                        "candle_observation_store_observation_time_regressed"
                    ),
                )
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    try:
        with pytest.raises(
            CandleObservationStoreError,
            match="candle_observation_store_observation_time_regressed",
        ):
            await store.append_observation(candle)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"status": "accepted"},
        {"revision": True},
        {"canonical_observation_sha256": "0" * 64},
        {"stored_observed_at": "not-a-time"},
        {"quarantine_id": QUARANTINE_ID},
        {
            "status": "quarantined",
            "inserted": False,
            "quarantine_id": QUARANTINE_ID,
            "reason_code": [],
        },
    ],
)
async def test_store_rejects_malformed_or_mismatched_receipt(
    mutation: dict[str, object],
) -> None:
    candle = _candle()

    async def handler(_request: httpx.Request) -> httpx.Response:
        row = _receipt(candle, inserted=True)
        row.update(mutation)
        return httpx.Response(200, json=[row])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    try:
        with pytest.raises(CandleObservationStoreError):
            await store.append_observation(candle)
    finally:
        await client.aclose()


@pytest.mark.parametrize("body", [[], [{}, {}], {}, [None]])
async def test_store_requires_exact_singleton_rpc_row(body: object) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    try:
        with pytest.raises(
            CandleObservationStoreError,
            match="candle_observation_store_rpc_result_invalid",
        ):
            await store.append_observation(_candle())
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
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    try:
        with pytest.raises(
            CandleObservationStoreError,
            match="rpc_failed_or_returned_invalid_json",
        ):
            await store.append_observation(_candle())
    finally:
        await client.aclose()


async def test_store_revalidates_candle_before_network() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCandleObservationStore(_settings(), client=client)
    tampered = _candle()
    object.__setattr__(tampered, "close_krw", 1)
    try:
        for invalid in (cast(Any, object()), tampered):
            with pytest.raises(
                CandleObservationStoreError,
                match="candle_observation_store_item_invalid",
            ):
                await store.append_observation(invalid)
    finally:
        await client.aclose()

    assert requests == 0


def test_store_requires_worker_credentials() -> None:
    with pytest.raises(
        CandleObservationStoreError,
        match="candle_observation_store_credentials_missing",
    ):
        SupabaseCandleObservationStore(Settings())


def _receipt(
    candle: Any,
    *,
    inserted: bool,
    status: str = "stored",
    stored_at: str | None = None,
    quarantine_id: str | None = None,
    reason_code: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "idempotency_key": candle.idempotency_key,
        "canonical_observation_sha256": candle.canonical_observation_sha256,
        "revision": 1,
        "inserted": inserted,
        "stored_observed_at": stored_at or candle.observed_at.isoformat(),
        "quarantine_id": quarantine_id,
        "reason_code": reason_code,
    }


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "SUPABASE_URL": "http://127.0.0.1:54321",
            "SUPABASE_SECRET_KEY": SecretStr("test-secret"),
        }
    )

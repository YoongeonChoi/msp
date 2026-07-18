from __future__ import annotations

import json
import traceback
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_calendar_observation_store import (
    PIT_KR_DAILY_SESSION_RPC_ALLOWLIST,
    SupabaseCalendarObservationStore,
)
from app.application.ports.calendar_observation_store_port import (
    CalendarObservationStoreError,
)
from app.config import Settings
from app.tests.unit.test_in_memory_calendar_observation_store import (
    OBSERVED_AT,
    _session,
)

OCCURRENCE_ID = "a7eeb600-d8d3-4e8b-a9b7-6330666c0590"
QUARANTINE_ID = "52a6e169-0467-49d9-a092-1d78e0a477ae"


@pytest.mark.parametrize("is_open", [True, False])
async def test_store_posts_canonical_open_or_closed_session_to_only_rpc(
    is_open: bool,
) -> None:
    session = _session(is_open=is_open)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["content-profile"] == "worker_api"
        assert request.url.path.endswith(
            "/append_pit_kr_daily_session_observation_v1"
        )
        assert payload == {"p_session": session.to_payload()}
        return httpx.Response(200, json=[_receipt(session)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCalendarObservationStore(_settings(), client=client)
    try:
        receipt = await store.append_observation(session)
    finally:
        await client.aclose()

    assert receipt.status == "stored"
    assert receipt.calendar_idempotency_key == session.idempotency_key
    assert receipt.canonical_evidence_sha256 == session.canonical_evidence_sha256
    assert receipt.revision == 1
    assert receipt.revision_inserted is True
    assert receipt.occurrence_id.version == 4
    assert receipt.occurrence_inserted is True
    assert receipt.observed_at == session.observed_at
    assert len(requests) == 1
    assert {
        "append_pit_kr_daily_session_observation_v1"
    } == PIT_KR_DAILY_SESSION_RPC_ALLOWLIST


@pytest.mark.parametrize("occurrence_inserted", [True, False])
async def test_store_accepts_strict_later_or_exact_replay_receipt(
    occurrence_inserted: bool,
) -> None:
    session = _session(observed_at=OBSERVED_AT + timedelta(minutes=5))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _receipt(
                    session,
                    status="replayed",
                    revision_inserted=False,
                    occurrence_inserted=occurrence_inserted,
                )
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCalendarObservationStore(_settings(), client=client)
    try:
        receipt = await store.append_observation(session)
    finally:
        await client.aclose()

    assert receipt.status == "replayed"
    assert receipt.revision_inserted is False
    assert receipt.occurrence_inserted is occurrence_inserted


@pytest.mark.parametrize(
    "reason",
    [
        "pit_calendar_observation_time_regressed",
        "pit_calendar_revision_time_not_increasing",
        "pit_calendar_historical_hash_recurrence_ambiguous",
    ],
)
async def test_store_turns_durable_quarantine_into_fail_closed_error(
    reason: str,
) -> None:
    session = _session()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _receipt(
                    session,
                    status="quarantined",
                    revision_inserted=False,
                    occurrence_id=None,
                    occurrence_inserted=False,
                    quarantine_id=QUARANTINE_ID,
                    reason_code=reason,
                )
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCalendarObservationStore(_settings(), client=client)
    try:
        with pytest.raises(CalendarObservationStoreError, match=reason):
            await store.append_observation(session)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"status": "accepted"},
        {"calendar_idempotency_key": "0" * 64},
        {"canonical_evidence_sha256": "0" * 64},
        {"revision": True},
        {"revision_inserted": "true"},
        {"occurrence_id": "not-a-uuid"},
        {"occurrence_inserted": 1},
        {"observed_at": "2026-03-25T07:00:00"},
        {"observed_at": "2026-03-25 07:00:00+00:00"},
        {"observed_at": "2026-03-25T07:00:00+0000"},
        {"observed_at": (OBSERVED_AT + timedelta(seconds=1)).isoformat()},
        {"quarantine_id": QUARANTINE_ID},
        {"reason_code": "unknown"},
        {"status": "stored", "revision_inserted": False},
        {
            "status": "quarantined",
            "revision_inserted": False,
            "occurrence_id": None,
            "occurrence_inserted": False,
            "quarantine_id": QUARANTINE_ID,
            "reason_code": "unknown",
        },
    ],
)
async def test_store_rejects_malformed_or_mismatched_receipt(
    mutation: dict[str, object],
) -> None:
    session = _session()

    async def handler(_request: httpx.Request) -> httpx.Response:
        row = _receipt(session)
        row.update(mutation)
        return httpx.Response(200, json=[row])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCalendarObservationStore(_settings(), client=client)
    try:
        with pytest.raises(CalendarObservationStoreError):
            await store.append_observation(session)
    finally:
        await client.aclose()


@pytest.mark.parametrize("body", [[], [{}, {}], {}, [None]])
async def test_store_requires_exact_singleton_rpc_row(body: object) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCalendarObservationStore(_settings(), client=client)
    try:
        with pytest.raises(
            CalendarObservationStoreError,
            match="calendar_observation_store_rpc_result_invalid",
        ):
            await store.append_observation(_session())
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="upstream secret=must-not-leak"),
        httpx.Response(200, content=b"secret-invalid-json"),
    ],
)
async def test_store_suppresses_secret_bearing_transport_or_json_chain(
    response: httpx.Response,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCalendarObservationStore(_settings(), client=client)
    try:
        with pytest.raises(CalendarObservationStoreError) as captured:
            await store.append_observation(_session())
    finally:
        await client.aclose()

    error = captured.value
    assert "must-not-leak" not in str(error)
    assert "secret-invalid-json" not in str(error)
    formatted = "".join(traceback.format_exception(error))
    assert "must-not-leak" not in formatted
    assert "secret-invalid-json" not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is False


async def test_store_revalidates_and_copies_input_before_network() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCalendarObservationStore(_settings(), client=client)
    tampered = _session()
    object.__setattr__(tampered, "provider_contract_sha256", "b" * 64)
    try:
        for invalid in (cast(Any, object()), tampered):
            with pytest.raises(
                CalendarObservationStoreError,
                match="calendar_observation_store_item_invalid",
            ):
                await store.append_observation(invalid)
    finally:
        await client.aclose()

    assert requests == 0


def test_store_requires_worker_credentials() -> None:
    with pytest.raises(
        CalendarObservationStoreError,
        match="calendar_observation_store_credentials_missing",
    ):
        SupabaseCalendarObservationStore(Settings())


def _receipt(
    session: Any,
    *,
    status: str = "stored",
    revision_inserted: bool = True,
    occurrence_id: str | None = OCCURRENCE_ID,
    occurrence_inserted: bool = True,
    quarantine_id: str | None = None,
    reason_code: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "calendar_idempotency_key": session.idempotency_key,
        "canonical_evidence_sha256": session.canonical_evidence_sha256,
        "revision": 1,
        "revision_inserted": revision_inserted,
        "occurrence_id": occurrence_id,
        "occurrence_inserted": occurrence_inserted,
        "observed_at": session.observed_at.isoformat(),
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

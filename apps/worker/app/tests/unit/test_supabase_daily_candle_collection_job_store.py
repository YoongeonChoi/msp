from __future__ import annotations

import asyncio
import gzip
import json
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_daily_candle_collection_job_store import (
    DAILY_CANDLE_COLLECTION_JOB_MAX_RPC_RESPONSE_BYTES,
    DAILY_CANDLE_COLLECTION_JOB_RPC_ALLOWLIST,
    DAILY_CANDLE_COLLECTION_JOB_SAFE_DATABASE_ERRORS,
    DAILY_CANDLE_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION,
    SupabaseDailyCandleCollectionJobStore,
)
from app.application.ports.candle_observation_store_port import (
    CandleObservationWriteReceipt,
)
from app.application.ports.daily_candle_collection_job_store_port import (
    DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_SCHEMA_VERSION,
    DailyCandleCollectionAttemptV1,
    DailyCandleCollectionCandidateV1,
    DailyCandleCollectionCompletionV1,
    DailyCandleCollectionJobSnapshotV1,
    DailyCandleCollectionJobSpecV1,
    DailyCandleCollectionJobStoreError,
    DailyCandleCollectionWriteEvidenceV1,
)
from app.config import Settings
from app.domain.market_data.point_in_time import PointInTimeCandleV1

JOB_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
HOLDER_ID = "33333333-3333-4333-8333-333333333333"
CONTENT_REVISION_ID = UUID("44444444-4444-4444-8444-444444444444")
OCCURRENCE_ID = UUID("55555555-5555-4555-8555-555555555555")
CONTRACT_SHA256 = "a" * 64
MAX_DATABASE_BIGINT = 9_223_372_036_854_775_807
EVENT_AT = datetime(2026, 3, 24, 6, tzinfo=UTC)
NOW = datetime(2026, 3, 25, 0, tzinfo=UTC)
OBSERVED_AT = NOW + timedelta(seconds=1)
FENCED_AT = NOW + timedelta(seconds=2)
CONFIRMED_AT = NOW + timedelta(seconds=3)


async def test_store_routes_all_rpc_transitions_and_binds_authoritative_evidence() -> None:
    spec = _spec()
    candle = _candle()
    receipt = _receipt(candle)
    responses = {
        "/rest/v1/rpc/load_or_create_pit_daily_candle_collection_job_v1": _rpc_response(
            _ready(spec)
        ),
        "/rest/v1/rpc/inspect_pit_daily_candle_collection_job_v1": _inspection_response(
            _ready(spec)
        ),
        "/rest/v1/rpc/begin_pit_daily_candle_collection_attempt_v1": _rpc_response(
            _collecting(spec)
        ),
        "/rest/v1/rpc/fence_pit_daily_candle_collection_candidate_v1": _rpc_response(
            _candidate_fenced(spec, candle=candle)
        ),
        "/rest/v1/rpc/pause_pit_daily_candle_collection_attempt_v1": _rpc_response(_paused(spec)),
        "/rest/v1/rpc/block_pit_daily_candle_collection_attempt_v1": _rpc_response(
            _blocked(spec, candle=candle)
        ),
        "/rest/v1/rpc/confirm_pit_daily_candle_collection_attempt_v1": _rpc_response(
            _completed(spec, candle=candle, receipt=receipt)
        ),
    }
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=responses[request.url.path])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        assert store.persistence_kind == "durable"
        assert await store.load_or_create_job(spec, now=NOW) == _ready(spec)
        assert await store.inspect_job(JOB_ID) == _ready(spec)
        assert await store.begin_attempt(
            **_transition_args(spec, expected_revision=1, now=NOW)
        ) == (_collecting(spec))
        assert await store.fence_candidate(
            **_transition_args(spec, expected_revision=2, now=FENCED_AT),
            fencing_revision=2,
            candle=candle,
        ) == _candidate_fenced(spec, candle=candle)
        assert await store.pause_retryable(
            **_transition_args(spec, expected_revision=2, now=FENCED_AT),
            fencing_revision=2,
            reason_code="provider_read_failed_before_candidate",
        ) == _paused(spec)
        assert await store.block_unknown(
            **_transition_args(spec, expected_revision=3, now=CONFIRMED_AT),
            fencing_revision=2,
            reason_code="append_outcome_unknown",
        ) == _blocked(spec, candle=candle)
        completed = await store.confirm_candidate(
            **_transition_args(spec, expected_revision=3, now=CONFIRMED_AT),
            fencing_revision=2,
            receipt=receipt,
        )
    finally:
        await client.aclose()

    assert completed == _completed(spec, candle=candle, receipt=receipt)
    assert completed.completion is not None
    assert completed.completion.write_evidence.content_revision_id == CONTENT_REVISION_ID
    assert completed.completion.write_evidence.occurrence_id == OCCURRENCE_ID
    assert len(requests) == 7
    assert all(request.method == "POST" for request in requests)
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)
    load_payload = json.loads(requests[0].content)
    assert load_payload["p_spec"] == _spec_payload(spec)
    fence_payload = json.loads(requests[3].content)
    assert "p_candle" not in fence_payload
    assert fence_payload["p_candidate"] == candle.to_payload()
    confirm_payload = json.loads(requests[-1].content)
    assert set(confirm_payload) == {
        "p_job_id",
        "p_spec_sha256",
        "p_expected_revision",
        "p_attempt_id",
        "p_holder_id",
        "p_now",
        "p_fencing_revision",
        "p_receipt",
    }
    assert "content_revision_id" not in json.dumps(confirm_payload)
    assert "occurrence_id" not in json.dumps(confirm_payload)


async def test_store_inspects_absent_job_only_from_exact_null_snapshot() -> None:
    responses = [
        [{"found": False, "snapshot": None}],
        [{"found": False, "snapshot": _snapshot_payload(_ready(_spec()))}],
    ]
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        assert await store.inspect_job(JOB_ID) is None
        with pytest.raises(DailyCandleCollectionJobStoreError):
            await store.inspect_job(JOB_ID)
    finally:
        await client.aclose()

    assert calls == 2


async def test_store_accepts_pre_candidate_unknown_block_and_replayed_receipt() -> None:
    spec = _spec()
    candle = _candle()
    replayed = _receipt(
        candle,
        inserted=False,
        stored_observed_at=EVENT_AT,
    )
    responses = [
        _rpc_response(_blocked(spec, candle=None, revision=3, updated_at=FENCED_AT)),
        _rpc_response(_completed(spec, candle=candle, receipt=replayed)),
    ]
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        pre_candidate = await store.block_unknown(
            **_transition_args(spec, expected_revision=2, now=FENCED_AT),
            fencing_revision=2,
            reason_code="unexpected_failure_before_candidate",
        )
        completed = await store.confirm_candidate(
            **_transition_args(spec, expected_revision=3, now=CONFIRMED_AT),
            fencing_revision=2,
            receipt=replayed,
        )
    finally:
        await client.aclose()

    assert pre_candidate.fenced_candidate is None
    assert completed.completion is not None
    assert completed.completion.write_evidence.receipt.inserted is False
    assert completed.completion.write_evidence.receipt.stored_observed_at == EVENT_AT
    assert completed.completion.write_evidence.occurrence_observed_at == OBSERVED_AT
    assert calls == 2


async def test_store_rejects_transition_revision_exhaustion_before_rpc() -> None:
    spec = _spec()
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(DailyCandleCollectionJobStoreError, match="revision_exhausted"):
            await store.begin_attempt(
                **_transition_args(
                    spec,
                    expected_revision=(DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION + 1),
                    now=NOW,
                )
            )
        with pytest.raises(DailyCandleCollectionJobStoreError, match="revision_exhausted"):
            await store.fence_candidate(
                **_transition_args(
                    spec,
                    expected_revision=(DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION + 1),
                    now=FENCED_AT,
                ),
                fencing_revision=(DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION + 1),
                candle=_candle(),
            )
        with pytest.raises(DailyCandleCollectionJobStoreError, match="revision_exhausted"):
            await store.pause_retryable(
                **_transition_args(
                    spec,
                    expected_revision=(DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION + 1),
                    now=FENCED_AT,
                ),
                fencing_revision=(DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION + 1),
                reason_code="provider_read_failed_before_candidate",
            )
        with pytest.raises(DailyCandleCollectionJobStoreError, match="revision_exhausted"):
            await store.block_unknown(
                **_transition_args(
                    spec,
                    expected_revision=(DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION + 1),
                    now=FENCED_AT,
                ),
                fencing_revision=MAX_DATABASE_BIGINT - 1,
                reason_code="unexpected_failure_before_candidate",
            )
        with pytest.raises(DailyCandleCollectionJobStoreError, match="revision_exhausted"):
            await store.confirm_candidate(
                **_transition_args(
                    spec,
                    expected_revision=(
                        DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION + 1
                    ),
                    now=CONFIRMED_AT,
                ),
                fencing_revision=DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION,
                receipt=_receipt(_candle()),
            )
        with pytest.raises(
            DailyCandleCollectionJobStoreError,
            match="daily_candle_collection_job_store_revision_invalid",
        ):
            await store.begin_attempt(
                **_transition_args(
                    spec,
                    expected_revision=MAX_DATABASE_BIGINT,
                    now=NOW,
                )
            )
    finally:
        await client.aclose()

    assert requests == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: _set_path(payload, (0, "snapshot", "unexpected"), True),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "spec", "pagination_allowed"),
            True,
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "candidate", "candle", "adjusted"),
            1,
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "candidate", "candle", "unexpected"),
            "secret",
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "completion", "receipt", "revision"),
            True,
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "completion", "occurrence_id"),
            str(CONTENT_REVISION_ID),
        ),
        lambda payload: _set_path(
            payload,
            (
                0,
                "snapshot",
                "completion",
                "occurrence_observed_at",
            ),
            _timestamp(OBSERVED_AT + timedelta(seconds=1)),
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "candidate", "idempotency_key"),
            "b" * 64,
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "completion", "content_revision"),
            2,
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "completion", "content_revision_observed_at"),
            _timestamp(NOW),
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "completion", "idempotency_key"),
            "b" * 64,
        ),
        lambda payload: _set_path(
            payload,
            (0, "snapshot", "completion", "persistence_kind"),
            "reference",
        ),
    ],
    ids=[
        "snapshot-extra",
        "pagination-enabled",
        "candle-bool-as-int",
        "candle-extra",
        "receipt-bool-as-int",
        "reused-database-identity",
        "occurrence-clock-mismatch",
        "candidate-identity-mismatch",
        "content-revision-mismatch",
        "content-revision-clock-mismatch",
        "completion-identity-mismatch",
        "completion-persistence-mismatch",
    ],
)
async def test_store_rejects_extra_fields_and_unbound_nested_response(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    spec = _spec()
    body = cast(dict[str, Any], _rpc_response(_completed(spec)))
    mutate(body)
    store, client = _store_for_body(body)
    try:
        with pytest.raises(DailyCandleCollectionJobStoreError):
            await store.load_or_create_job(spec, now=NOW)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(b"secret-compressed-body"),
        ),
        httpx.Response(
            200,
            content=b" " * (DAILY_CANDLE_COLLECTION_JOB_MAX_RPC_RESPONSE_BYTES + 1),
        ),
        httpx.Response(200, content=b'[{"snapshot":null,"snapshot":null}]'),
        httpx.Response(200, content=b'[{"snapshot":NaN}]'),
        httpx.Response(200, content=b"[" * 10_000 + b"0" + b"]" * 10_000),
    ],
    ids=["compressed", "oversized", "duplicate", "nan", "overnested"],
)
async def test_store_rejects_unsafe_bounded_json_without_leaking_body(
    response: httpx.Response,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(DailyCandleCollectionJobStoreError) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()

    formatted = "".join(traceback.format_exception(captured.value))
    assert "secret-compressed-body" not in formatted
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert calls == 1


async def test_store_maps_only_allowlisted_database_messages() -> None:
    safe = "daily_candle_collection_job_revision_conflict"
    assert safe in DAILY_CANDLE_COLLECTION_JOB_SAFE_DATABASE_ERRORS
    responses = [
        httpx.Response(
            409,
            json={
                "code": "PT409",
                "details": None,
                "hint": None,
                "message": f"pit_{safe}",
            },
        ),
        httpx.Response(409, json={"message": "database-secret-must-not-leak"}),
    ]
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(DailyCandleCollectionJobStoreError, match=safe):
            await store.load_or_create_job(_spec(), now=NOW)
        with pytest.raises(DailyCandleCollectionJobStoreError) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()

    assert str(captured.value) == (
        "daily_candle_collection_job_store_rpc_failed_or_returned_invalid_json"
    )
    assert "database-secret" not in "".join(traceback.format_exception(captured.value))
    assert calls == 2


@pytest.mark.parametrize("failure", ["transport", "runtime", "invalid-json", "http"])
async def test_store_suppresses_failure_chain_and_never_retries(failure: str) -> None:
    secret = "upstream-secret-must-not-leak"
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "transport":
            raise httpx.ReadError(secret, request=request)
        if failure == "runtime":
            raise RuntimeError(secret)
        if failure == "invalid-json":
            return httpx.Response(200, content=secret.encode())
        return httpx.Response(503, content=secret.encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(DailyCandleCollectionJobStoreError) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()

    formatted = "".join(traceback.format_exception(captured.value))
    assert secret not in formatted
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert calls == 1


async def test_store_propagates_cancellation_without_retry() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(asyncio.CancelledError):
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    assert calls == 1


async def test_store_rejects_invalid_inputs_before_network() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    spec = _spec()
    invalid_calls: list[Callable[[], Awaitable[object]]] = [
        lambda: store.begin_attempt(
            **_transition_args(spec, expected_revision=cast(Any, True), now=NOW)
        ),
        lambda: store.fence_candidate(
            **_transition_args(spec, expected_revision=2, now=FENCED_AT),
            fencing_revision=4,
            candle=_candle(),
        ),
        lambda: store.pause_retryable(
            **_transition_args(spec, expected_revision=2, now=FENCED_AT),
            fencing_revision=2,
            reason_code="INVALID-REASON",
        ),
        lambda: store.pause_retryable(
            **_transition_args(spec, expected_revision=2, now=FENCED_AT),
            fencing_revision=2,
            reason_code="unexpected_failure_before_candidate",
        ),
        lambda: store.block_unknown(
            **_transition_args(spec, expected_revision=2, now=FENCED_AT),
            fencing_revision=2,
            reason_code="append_outcome_unknown",
        ),
        lambda: store.block_unknown(
            **_transition_args(spec, expected_revision=3, now=CONFIRMED_AT),
            fencing_revision=2,
            reason_code="unexpected_failure_before_candidate",
        ),
        lambda: store.confirm_candidate(
            **_transition_args(spec, expected_revision=3, now=CONFIRMED_AT),
            fencing_revision=2,
            receipt=cast(Any, object()),
        ),
        lambda: store._rpc(cast(Any, "delete_everything"), {}),
    ]
    try:
        for call in invalid_calls:
            with pytest.raises(DailyCandleCollectionJobStoreError):
                await call()
    finally:
        await client.aclose()
    assert requests == 0


async def test_store_returns_detached_canonical_snapshots() -> None:
    original = _ready(_spec())
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_rpc_response(original))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseDailyCandleCollectionJobStore(_settings(), client=client)
    try:
        first = await store.load_or_create_job(_spec(), now=NOW)
        object.__setattr__(first, "revision", 999)
        object.__setattr__(first.spec, "provider", "tampered")
        second = await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()

    assert second == original
    assert second.revision == 1
    assert second.spec.provider == "toss"
    assert calls == 2


def test_store_has_fixed_rpc_allowlist_and_requires_credentials() -> None:
    assert {
        "load_or_create_pit_daily_candle_collection_job_v1",
        "inspect_pit_daily_candle_collection_job_v1",
        "begin_pit_daily_candle_collection_attempt_v1",
        "fence_pit_daily_candle_collection_candidate_v1",
        "pause_pit_daily_candle_collection_attempt_v1",
        "block_pit_daily_candle_collection_attempt_v1",
        "confirm_pit_daily_candle_collection_attempt_v1",
    } == DAILY_CANDLE_COLLECTION_JOB_RPC_ALLOWLIST
    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="credentials_missing",
    ):
        SupabaseDailyCandleCollectionJobStore(Settings())


async def test_store_closes_only_owned_client() -> None:
    owned = SupabaseDailyCandleCollectionJobStore(_settings())
    assert owned.client.is_closed is False
    await owned.close()
    assert owned.client.is_closed is True

    external_client = httpx.AsyncClient()
    external = SupabaseDailyCandleCollectionJobStore(_settings(), client=external_client)
    await external.close()
    assert external_client.is_closed is False
    await external_client.aclose()


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "SUPABASE_URL": "http://127.0.0.1:54321",
            "SUPABASE_SECRET_KEY": SecretStr("test-secret"),
        }
    )


def _spec() -> DailyCandleCollectionJobSpecV1:
    return DailyCandleCollectionJobSpecV1(
        job_id=JOB_ID,
        provider="toss",
        symbol="005930",
        market="KR",
        interval="1d",
        adjusted=True,
        before=EVENT_AT,
        provider_contract_sha256=CONTRACT_SHA256,
        trigger="manual",
    )


def _candle() -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider="toss",
        symbol="005930",
        market="KR",
        interval="1d",
        adjusted=True,
        provider_event_at=EVENT_AT,
        observed_at=OBSERVED_AT,
        currency="KRW",
        open_krw=71_600,
        high_krw=72_300,
        low_krw=71_500,
        close_krw=72_000,
        volume=3_521_000,
        provider_contract_sha256=CONTRACT_SHA256,
    )


def _receipt(
    candle: PointInTimeCandleV1,
    *,
    inserted: bool = True,
    stored_observed_at: datetime | None = None,
) -> CandleObservationWriteReceipt:
    return CandleObservationWriteReceipt(
        idempotency_key=candle.idempotency_key,
        canonical_observation_sha256=candle.canonical_observation_sha256,
        revision=1,
        inserted=inserted,
        stored_observed_at=stored_observed_at or candle.observed_at,
    )


def _write_evidence(
    candle: PointInTimeCandleV1,
    receipt: CandleObservationWriteReceipt,
) -> DailyCandleCollectionWriteEvidenceV1:
    return DailyCandleCollectionWriteEvidenceV1(
        persistence_kind="durable",
        receipt=receipt,
        content_revision_id=CONTENT_REVISION_ID,
        occurrence_id=OCCURRENCE_ID,
        occurrence_observed_at=candle.observed_at,
    )


def _attempt() -> DailyCandleCollectionAttemptV1:
    return DailyCandleCollectionAttemptV1(
        attempt_id=ATTEMPT_ID,
        holder_id=HOLDER_ID,
        fencing_revision=2,
        begun_at=NOW,
    )


def _candidate(candle: PointInTimeCandleV1 | None = None) -> DailyCandleCollectionCandidateV1:
    return DailyCandleCollectionCandidateV1(
        attempt=_attempt(),
        candle=candle or _candle(),
        fenced_at=FENCED_AT,
    )


def _ready(spec: DailyCandleCollectionJobSpecV1) -> DailyCandleCollectionJobSnapshotV1:
    return DailyCandleCollectionJobSnapshotV1(
        spec=spec,
        revision=1,
        state="ready",
        active_attempt=None,
        fenced_candidate=None,
        completion=None,
        state_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _collecting(spec: DailyCandleCollectionJobSpecV1) -> DailyCandleCollectionJobSnapshotV1:
    return DailyCandleCollectionJobSnapshotV1(
        spec=spec,
        revision=2,
        state="collecting",
        active_attempt=_attempt(),
        fenced_candidate=None,
        completion=None,
        state_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _candidate_fenced(
    spec: DailyCandleCollectionJobSpecV1,
    *,
    candle: PointInTimeCandleV1 | None = None,
) -> DailyCandleCollectionJobSnapshotV1:
    candidate = _candidate(candle)
    return DailyCandleCollectionJobSnapshotV1(
        spec=spec,
        revision=3,
        state="candidate_fenced",
        active_attempt=candidate.attempt,
        fenced_candidate=candidate,
        completion=None,
        state_reason=None,
        created_at=NOW,
        updated_at=FENCED_AT,
    )


def _paused(spec: DailyCandleCollectionJobSpecV1) -> DailyCandleCollectionJobSnapshotV1:
    return DailyCandleCollectionJobSnapshotV1(
        spec=spec,
        revision=3,
        state="paused_retryable",
        active_attempt=None,
        fenced_candidate=None,
        completion=None,
        state_reason="provider_read_failed_before_candidate",
        created_at=NOW,
        updated_at=FENCED_AT,
    )


def _blocked(
    spec: DailyCandleCollectionJobSpecV1,
    *,
    candle: PointInTimeCandleV1 | None,
    revision: int = 4,
    updated_at: datetime = CONFIRMED_AT,
) -> DailyCandleCollectionJobSnapshotV1:
    return DailyCandleCollectionJobSnapshotV1(
        spec=spec,
        revision=revision,
        state="blocked_unknown",
        active_attempt=_attempt(),
        fenced_candidate=None if candle is None else _candidate(candle),
        completion=None,
        state_reason=(
            "unexpected_failure_before_candidate" if candle is None else "append_outcome_unknown"
        ),
        created_at=NOW,
        updated_at=updated_at,
    )


def _completed(
    spec: DailyCandleCollectionJobSpecV1,
    *,
    candle: PointInTimeCandleV1 | None = None,
    receipt: CandleObservationWriteReceipt | None = None,
) -> DailyCandleCollectionJobSnapshotV1:
    resolved_candle = candle or _candle()
    resolved_receipt = receipt or _receipt(resolved_candle)
    completion = DailyCandleCollectionCompletionV1(
        candidate=_candidate(resolved_candle),
        write_evidence=_write_evidence(resolved_candle, resolved_receipt),
        confirmed_at=CONFIRMED_AT,
    )
    return DailyCandleCollectionJobSnapshotV1(
        spec=spec,
        revision=4,
        state="completed",
        active_attempt=None,
        fenced_candidate=None,
        completion=completion,
        state_reason=None,
        created_at=NOW,
        updated_at=CONFIRMED_AT,
    )


def _transition_args(
    spec: DailyCandleCollectionJobSpecV1,
    *,
    expected_revision: Any,
    now: datetime,
) -> dict[str, Any]:
    return {
        "job_id": spec.job_id,
        "spec_sha256": spec.spec_sha256,
        "expected_revision": expected_revision,
        "attempt_id": ATTEMPT_ID,
        "holder_id": HOLDER_ID,
        "now": now,
    }


def _rpc_response(snapshot: DailyCandleCollectionJobSnapshotV1) -> list[object]:
    return [{"snapshot": _snapshot_payload(snapshot)}]


def _inspection_response(snapshot: DailyCandleCollectionJobSnapshotV1) -> list[object]:
    return [{"found": True, "snapshot": _snapshot_payload(snapshot)}]


def _snapshot_payload(snapshot: DailyCandleCollectionJobSnapshotV1) -> dict[str, object]:
    candidate = snapshot.fenced_candidate
    if candidate is None and snapshot.completion is not None:
        candidate = snapshot.completion.candidate
    return {
        "schema_version": DAILY_CANDLE_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION,
        "spec_sha256": snapshot.spec.spec_sha256,
        "spec": _spec_payload(snapshot.spec),
        "revision": snapshot.revision,
        "state": snapshot.state,
        "active_attempt": (
            None if snapshot.active_attempt is None else _attempt_payload(snapshot.active_attempt)
        ),
        "candidate": None if candidate is None else _candidate_payload(candidate),
        "completion": (
            None if snapshot.completion is None else _completion_payload(snapshot.completion)
        ),
        "state_reason": snapshot.state_reason,
        "created_at": _timestamp(snapshot.created_at),
        "updated_at": _timestamp(snapshot.updated_at),
        "automatic_retry_allowed": False,
    }


def _spec_payload(spec: DailyCandleCollectionJobSpecV1) -> dict[str, object]:
    return {
        "schema_version": DAILY_CANDLE_COLLECTION_JOB_SCHEMA_VERSION,
        "job_id": spec.job_id,
        "provider": spec.provider,
        "symbol": spec.symbol,
        "market": spec.market,
        "interval": spec.interval,
        "adjusted": spec.adjusted,
        "before": _timestamp(spec.before),
        "provider_contract_sha256": spec.provider_contract_sha256,
        "trigger": spec.trigger,
        "count": 1,
        "pagination_allowed": False,
        "automatic_retry_allowed": False,
    }


def _attempt_payload(attempt: DailyCandleCollectionAttemptV1) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id,
        "holder_id": attempt.holder_id,
        "fencing_revision": attempt.fencing_revision,
        "begun_at": _timestamp(attempt.begun_at),
    }


def _candidate_payload(candidate: DailyCandleCollectionCandidateV1) -> dict[str, object]:
    return {
        "attempt_id": candidate.attempt.attempt_id,
        "holder_id": candidate.attempt.holder_id,
        "fencing_revision": candidate.attempt.fencing_revision,
        "begun_at": _timestamp(candidate.attempt.begun_at),
        "idempotency_key": candidate.candle.idempotency_key,
        "canonical_observation_sha256": candidate.candle.canonical_observation_sha256,
        "candle": candidate.candle.to_payload(),
        "fenced_at": _timestamp(candidate.fenced_at),
    }


def _receipt_payload(receipt: CandleObservationWriteReceipt) -> dict[str, object]:
    return {
        "idempotency_key": receipt.idempotency_key,
        "canonical_observation_sha256": receipt.canonical_observation_sha256,
        "revision": receipt.revision,
        "inserted": receipt.inserted,
        "stored_observed_at": _timestamp(receipt.stored_observed_at),
    }


def _completion_payload(completion: DailyCandleCollectionCompletionV1) -> dict[str, object]:
    evidence = completion.write_evidence
    return {
        "persistence_kind": evidence.persistence_kind,
        "occurrence_id": str(evidence.occurrence_id),
        "content_revision_id": str(evidence.content_revision_id),
        "occurrence_observed_at": _timestamp(evidence.occurrence_observed_at),
        "content_revision": evidence.receipt.revision,
        "content_revision_observed_at": _timestamp(evidence.receipt.stored_observed_at),
        "idempotency_key": evidence.receipt.idempotency_key,
        "canonical_observation_sha256": evidence.receipt.canonical_observation_sha256,
        "receipt": _receipt_payload(evidence.receipt),
        "confirmed_at": _timestamp(completion.confirmed_at),
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _store_for_body(
    body: object,
) -> tuple[SupabaseDailyCandleCollectionJobStore, httpx.AsyncClient]:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SupabaseDailyCandleCollectionJobStore(_settings(), client=client), client


def _set_path(
    payload: dict[str, Any],
    path: tuple[str | int, ...],
    value: object,
) -> None:
    current: Any = payload
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value

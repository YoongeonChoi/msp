from __future__ import annotations

import asyncio
import gzip
import json
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

import app.adapters.persistence.supabase_kr_calendar_collection_job_store as store_module
from app.adapters.persistence.supabase_kr_calendar_collection_job_store import (
    KR_CALENDAR_COLLECTION_JOB_RPC_ALLOWLIST,
    KR_CALENDAR_COLLECTION_JOB_SAFE_DATABASE_ERRORS,
    KR_CALENDAR_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION,
    SupabaseKrCalendarCollectionJobStore,
)
from app.application.ports.calendar_observation_store_port import (
    CalendarObservationWriteReceipt,
)
from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionDateAttemptV1,
    KrCalendarCollectionDateCheckpointV1,
    KrCalendarCollectionJobInspectorPort,
    KrCalendarCollectionJobSnapshotV1,
    KrCalendarCollectionJobSpecV1,
    KrCalendarCollectionJobStoreError,
    kr_calendar_collection_job_manifest_sha256,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
)
from app.config import Settings
from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

JOB_ID = "00000000-0000-4000-8000-000000000201"
OTHER_JOB_ID = "00000000-0000-4000-8000-000000000202"
HOLDER_ID = "00000000-0000-4000-8000-000000000203"
ATTEMPT_ID = "00000000-0000-4000-8000-000000000204"


class _TextSubclass(str):
    pass


OTHER_ATTEMPT_ID = "00000000-0000-4000-8000-000000000205"
OCCURRENCE_ID = UUID("00000000-0000-4000-8000-000000000206")
START_DATE = date(2026, 3, 25)
NOW = datetime(2026, 3, 24, 22, 0, tzinfo=UTC)
FINISHED_AT = NOW + timedelta(seconds=1)
CONTRACT_SHA256 = "c" * 64


async def test_store_uses_exact_rpc_payloads_and_reconstructs_every_transition() -> None:
    spec = _spec()
    collection = _collection()
    ready = _ready(spec)
    active = _active(spec)
    paused = _paused(spec)
    blocked = _blocked(spec)
    completed = _completed(spec, collection=collection)
    requests: list[httpx.Request] = []
    responses = {
        "load_or_create_kr_calendar_collection_job_v1": ready,
        "begin_kr_calendar_collection_date_attempt_v1": active,
        "pause_kr_calendar_collection_date_attempt_v1": paused,
        "block_kr_calendar_collection_date_attempt_v1": blocked,
        "confirm_kr_calendar_collection_date_v1": completed,
        "inspect_kr_calendar_collection_job_v1": ready,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        rpc = request.url.path.rsplit("/", 1)[-1]
        body = (
            _inspection_rpc_response(responses[rpc])
            if rpc == "inspect_kr_calendar_collection_job_v1"
            else _rpc_response(responses[rpc])
        )
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    inspector: KrCalendarCollectionJobInspectorPort = store
    try:
        loaded = await store.load_or_create_job(spec, now=NOW)
        begun = await store.begin_date_attempt(
            **_transition_args(spec, expected_revision=1, now=NOW)
        )
        paused_result = await store.pause_retryable(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            reason_code="collection_failed_before_write",
        )
        blocked_result = await store.block_unknown(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            reason_code="collection_write_outcome_unknown",
        )
        confirmed = await store.confirm_date(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            collection=collection,
        )
        inspected = await inspector.inspect_job(JOB_ID)
        await store.close()
        assert client.is_closed is False
    finally:
        await client.aclose()

    assert loaded == ready
    assert begun == active
    assert paused_result == paused
    assert blocked_result == blocked
    assert confirmed == completed
    assert inspected == ready
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == list(
        responses
    )
    assert frozenset(responses) == KR_CALENDAR_COLLECTION_JOB_RPC_ALLOWLIST
    for request in requests:
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["content-profile"] == "worker_api"
        assert request.headers["accept-encoding"] == "identity"

    base_payload = {
        "p_job_id": JOB_ID,
        "p_spec_sha256": spec.spec_sha256,
        "p_expected_revision": 2,
        "p_attempt_id": ATTEMPT_ID,
        "p_holder_id": HOLDER_ID,
        "p_target_date": START_DATE.isoformat(),
        "p_now": _timestamp(FINISHED_AT),
    }
    payloads = [json.loads(request.content) for request in requests]
    assert payloads[0] == {
        "p_spec": _spec_payload(spec),
        "p_now": _timestamp(NOW),
    }
    assert payloads[1] == {
        **base_payload,
        "p_expected_revision": 1,
        "p_now": _timestamp(NOW),
    }
    assert payloads[2] == {
        **base_payload,
        "p_reason_code": "collection_failed_before_write",
    }
    assert payloads[3] == {
        **base_payload,
        "p_reason_code": "collection_write_outcome_unknown",
    }
    assert payloads[4] == {
        **base_payload,
        "p_session": collection.session.to_payload(),
        "p_receipt": _receipt_payload(collection.receipt),
    }
    assert payloads[5] == {"p_job_id": JOB_ID}


async def test_inspect_returns_none_for_exact_not_found_envelope() -> None:
    store, client = _store_for_body(
        [{"job_found": False, "snapshot": None}]
    )
    try:
        assert await store.inspect_job(JOB_ID) is None
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        [],
        [
            {"job_found": False, "snapshot": None},
            {"job_found": False, "snapshot": None},
        ],
        [None],
        [{"snapshot": None}],
        [{"job_found": False}],
        [{"job_found": False, "snapshot": None, "unexpected": True}],
        [{"job_found": 0, "snapshot": None}],
        [{"job_found": "false", "snapshot": None}],
        [{"job_found": True, "snapshot": None}],
        [{"job_found": False, "snapshot": {}}],
    ],
)
async def test_inspect_requires_exact_singleton_found_snapshot_envelope(
    body: object,
) -> None:
    store, client = _store_for_body(body)
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="rpc_result_invalid",
        ):
            await store.inspect_job(JOB_ID)
    finally:
        await client.aclose()


async def test_inspect_revalidates_found_snapshot_and_job_binding() -> None:
    malformed_store, malformed_client = _store_for_body(
        [{"job_found": True, "snapshot": {}}]
    )
    wrong_job_store, wrong_job_client = _store_for_body(
        _inspection_rpc_response(_ready(_spec(job_id=OTHER_JOB_ID)))
    )
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="rpc_snapshot_invalid",
        ):
            await malformed_store.inspect_job(JOB_ID)
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="response_binding_invalid",
        ):
            await wrong_job_store.inspect_job(JOB_ID)
    finally:
        await malformed_client.aclose()
        await wrong_job_client.aclose()


@pytest.mark.parametrize(
    "job_id",
    [
        cast(Any, None),
        cast(Any, UUID(JOB_ID)),
        _TextSubclass(JOB_ID),
        "invalid",
        "00000000-0000-1000-8000-000000000201",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
async def test_inspect_rejects_noncanonical_uuid4_before_network(job_id: str) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="job_id_invalid",
        ):
            await store.inspect_job(job_id)
    finally:
        await client.aclose()
    assert requests == 0


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        [],
        [{"snapshot": {}}, {"snapshot": {}}],
        [{"snapshot": {}, "unexpected": True}],
        [None],
    ],
)
async def test_store_requires_exact_singleton_snapshot_row(body: object) -> None:
    store, client = _store_for_body(body)
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="rpc_result_invalid",
        ):
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unexpected",), True),
        (("schema_version",), "wrong.v1"),
        (("spec_sha256",), "f" * 64),
        (("revision",), True),
        (("state",), "accepted"),
        (("checkpoints",), {}),
        (("active_attempt",), []),
        (("state_reason",), 1),
        (("terminal_manifest_sha256",), "not-a-hash"),
        (("created_at",), "2026-03-24T22:00:00"),
        (("updated_at",), "2026-03-24 22:00:00+00:00"),
        (("updated_at",), "2026-03-24T22:00:00+00:00"),
        (("automatic_retry_allowed",), True),
        (("spec", "unexpected"), True),
        (("spec", "schema_version"), "wrong.v1"),
        (("spec", "job_id"), "not-a-uuid"),
        (("spec", "market"), "US"),
        (("spec", "start_date"), "2026-3-25"),
        (("spec", "trigger"), "automatic"),
    ],
)
async def test_store_rejects_malformed_snapshot_fields(
    path: tuple[str, ...],
    value: object,
) -> None:
    payload = _snapshot_payload(_ready(_spec()))
    _set_path(payload, path, value)
    store, client = _store_for_body([{"snapshot": payload}])
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="rpc_snapshot_invalid",
        ):
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("active_attempt", "unexpected"), True),
        (("active_attempt", "attempt_id"), "not-a-uuid"),
        (("active_attempt", "holder_id"), "not-a-uuid"),
        (("active_attempt", "target_date"), "2026-3-25"),
        (("active_attempt", "fencing_revision"), True),
        (("active_attempt", "begun_at"), "2026-03-24T22:00:00+0000"),
    ],
)
async def test_store_requires_exact_canonical_active_attempt(
    path: tuple[str, ...],
    value: object,
) -> None:
    payload = _snapshot_payload(_active(_spec()))
    _set_path(payload, path, value)
    store, client = _store_for_body([{"snapshot": payload}])
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="rpc_snapshot_invalid",
        ):
            await store.begin_date_attempt(
                **_transition_args(_spec(), expected_revision=1, now=NOW)
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("checkpoints", 0, "unexpected"), True),
        (("checkpoints", 0, "attempt_id"), "not-a-uuid"),
        (("checkpoints", 0, "fencing_revision"), True),
        (("checkpoints", 0, "begun_at"), "2026-03-24T22:00:00+0000"),
        (("checkpoints", 0, "session", "canonical_evidence_sha256"), "f" * 64),
        (("checkpoints", 0, "receipt", "revision"), True),
        (("checkpoints", 0, "receipt", "occurrence_id"), "not-a-uuid"),
        (("checkpoints", 0, "receipt", "observed_at"), "secret-time"),
        (("terminal_manifest_sha256",), "f" * 64),
    ],
)
async def test_store_revalidates_checkpoint_evidence_and_terminal_manifest(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    payload = _snapshot_payload(_completed(_spec(), collection=_collection()))
    _set_path(payload, path, value)
    store, client = _store_for_body([{"snapshot": payload}])
    try:
        with pytest.raises(KrCalendarCollectionJobStoreError):
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()


async def test_store_suppresses_secret_bearing_nested_parser_errors() -> None:
    secret = "malformed-secret-must-not-leak"
    payload = _snapshot_payload(_completed(_spec(), collection=_collection()))
    _set_path(payload, ("checkpoints", 0, "receipt", "observed_at"), secret)
    store, client = _store_for_body([{"snapshot": payload}])
    try:
        with pytest.raises(KrCalendarCollectionJobStoreError) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    error = captured.value
    assert secret not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "case",
    ["load", "begin", "pause", "block", "confirm"],
)
async def test_store_rejects_valid_but_wrong_response_binding(case: str) -> None:
    spec = _spec()
    collection = _collection()
    if case == "load":
        response = _ready(_spec(job_id=OTHER_JOB_ID))
    elif case == "begin":
        response = _active(spec, attempt_id=OTHER_ATTEMPT_ID)
    elif case == "pause":
        response = _paused(spec, updated_at=FINISHED_AT + timedelta(seconds=1))
    elif case == "block":
        response = _blocked(spec, attempt_id=OTHER_ATTEMPT_ID)
    else:
        response = _completed(
            spec,
            collection=collection,
            attempt_id=OTHER_ATTEMPT_ID,
        )
    store, client = _store_for_body(_rpc_response(response))
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="response_binding_invalid",
        ):
            await _invoke_case(store, case, spec, collection)
    finally:
        await client.aclose()


async def test_pause_response_must_retain_the_requested_target_progress() -> None:
    spec = _spec(end_date=START_DATE + timedelta(days=1))
    collection = _collection()
    checkpoint = KrCalendarCollectionDateCheckpointV1(
        attempt_id=ATTEMPT_ID,
        holder_id=HOLDER_ID,
        target_date=START_DATE,
        fencing_revision=2,
        begun_at=NOW,
        collection=collection,
        confirmed_at=FINISHED_AT,
    )
    wrong_progress = KrCalendarCollectionJobSnapshotV1(
        spec=spec,
        revision=5,
        state="paused_retryable",
        checkpoints=(checkpoint,),
        active_attempt=None,
        state_reason="collection_failed_before_write",
        terminal_manifest_sha256=None,
        created_at=NOW,
        updated_at=FINISHED_AT + timedelta(seconds=1),
    )
    store, client = _store_for_body(_rpc_response(wrong_progress))
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="response_binding_invalid",
        ):
            await store.pause_retryable(
                **_transition_args(
                    spec,
                    expected_revision=4,
                    now=FINISHED_AT + timedelta(seconds=1),
                ),
                reason_code="collection_failed_before_write",
            )
    finally:
        await client.aclose()


async def test_snapshot_bounds_checkpoint_count_before_item_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _snapshot_payload(_ready(_spec()))
    payload["checkpoints"] = [{}, {}]

    def unexpected_parse(_value: object) -> KrCalendarCollectionDateCheckpointV1:
        pytest.fail("checkpoint parser must not run beyond the bounded job range")

    monkeypatch.setattr(store_module, "_parse_checkpoint", unexpected_parse)
    store, client = _store_for_body([{"snapshot": payload}])
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="rpc_snapshot_invalid",
        ):
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()


async def test_store_revalidates_inputs_before_network() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    spec = _spec()
    tampered_spec = _spec()
    object.__setattr__(tampered_spec, "provider", "INVALID")
    tampered_collection = _collection()
    object.__setattr__(
        tampered_collection.receipt,
        "calendar_idempotency_key",
        "f" * 64,
    )
    invalid_calls: list[Callable[[], Awaitable[object]]] = [
        lambda: store.load_or_create_job(tampered_spec, now=NOW),
        lambda: store.load_or_create_job(spec, now=datetime(2026, 3, 24, 22)),
        lambda: store.begin_date_attempt(
            **_transition_args(spec, expected_revision=1, now=NOW)
            | {"job_id": "invalid"}
        ),
        lambda: store.begin_date_attempt(
            **_transition_args(spec, expected_revision=cast(Any, True), now=NOW)
        ),
        lambda: store.begin_date_attempt(
            **_transition_args(spec, expected_revision=1, now=NOW)
            | {"target_date": cast(Any, datetime(2026, 3, 25, tzinfo=UTC))}
        ),
        lambda: store.pause_retryable(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            reason_code="INVALID-REASON",
        ),
        lambda: store.confirm_date(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            collection=tampered_collection,
        ),
        lambda: store.confirm_date(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            collection=_collection(session_date=START_DATE + timedelta(days=1)),
        ),
    ]
    try:
        for call in invalid_calls:
            with pytest.raises(KrCalendarCollectionJobStoreError):
                await call()
    finally:
        await client.aclose()
    assert requests == 0


async def test_store_maps_only_allowlisted_database_messages() -> None:
    safe = "kr_calendar_collection_job_revision_conflict"
    assert safe in KR_CALENDAR_COLLECTION_JOB_SAFE_DATABASE_ERRORS
    responses = [
        httpx.Response(409, json={"message": safe}),
        httpx.Response(
            409,
            json={"message": "database-secret-must-not-leak"},
        ),
    ]
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(KrCalendarCollectionJobStoreError, match=safe):
            await store.load_or_create_job(_spec(), now=NOW)
        with pytest.raises(KrCalendarCollectionJobStoreError) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    assert str(captured.value) == (
        "kr_calendar_collection_job_store_rpc_failed_or_returned_invalid_json"
    )
    assert "database-secret" not in "".join(traceback.format_exception(captured.value))
    assert calls == 2


@pytest.mark.parametrize(
    "failure",
    ["transport", "runtime", "invalid_json", "http"],
)
async def test_store_suppresses_secret_bearing_failure_chain_and_never_retries(
    failure: str,
) -> None:
    secret = "upstream-secret-must-not-leak"
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if failure == "transport":
            raise httpx.ConnectError(secret, request=request)
        if failure == "runtime":
            raise RuntimeError(secret)
        if failure == "invalid_json":
            return httpx.Response(200, text=secret)
        return httpx.Response(503, text=secret)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(KrCalendarCollectionJobStoreError) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    error = captured.value
    formatted = "".join(traceback.format_exception(error))
    assert secret not in str(error)
    assert secret not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is False
    assert requests == 1


async def test_store_never_retries_an_ambiguous_post_commit_read_loss() -> None:
    secret = "committed-response-secret-must-not-leak"
    committed = False
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal committed, requests
        requests += 1
        committed = True
        raise httpx.ReadError(secret, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(KrCalendarCollectionJobStoreError) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    assert committed is True
    assert requests == 1
    assert secret not in "".join(traceback.format_exception(captured.value))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


async def test_store_rejects_duplicate_json_keys() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=b'[{"snapshot":null,"snapshot":null}]',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="failed_or_returned_invalid_json",
        ) as captured:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert requests == 1


async def test_store_propagates_cancellation_without_retry() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(asyncio.CancelledError):
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    assert requests == 1


async def test_store_rejects_compression_and_bounds_response_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "oversized-secret-must-not-leak"
    monkeypatch.setattr(store_module, "_MAX_RPC_RESPONSE_BYTES", 32)
    responses = [
        httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(secret.encode("utf-8")),
        ),
        httpx.Response(200, content=(secret * 10).encode("utf-8")),
    ]
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
    try:
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="content_encoding_invalid",
        ) as compressed:
            await store.load_or_create_job(_spec(), now=NOW)
        with pytest.raises(
            KrCalendarCollectionJobStoreError,
            match="response_too_large",
        ) as oversized:
            await store.load_or_create_job(_spec(), now=NOW)
    finally:
        await client.aclose()
    for error in (compressed.value, oversized.value):
        assert secret not in "".join(traceback.format_exception(error))
        assert error.__cause__ is None
        assert error.__context__ is None
    assert calls == 2


async def test_store_returns_detached_canonical_snapshots() -> None:
    original = _ready(_spec())
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_rpc_response(original))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseKrCalendarCollectionJobStore(_settings(), client=client)
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


def test_store_requires_credentials() -> None:
    with pytest.raises(
        KrCalendarCollectionJobStoreError,
        match="credentials_missing",
    ):
        SupabaseKrCalendarCollectionJobStore(Settings())


async def test_store_closes_only_owned_client() -> None:
    store = SupabaseKrCalendarCollectionJobStore(_settings())
    assert store.client.is_closed is False
    await store.close()
    assert store.client.is_closed is True


async def _invoke_case(
    store: SupabaseKrCalendarCollectionJobStore,
    case: str,
    spec: KrCalendarCollectionJobSpecV1,
    collection: CollectedKrDailySessionObservationV1,
) -> object:
    if case == "load":
        return await store.load_or_create_job(spec, now=NOW)
    if case == "begin":
        return await store.begin_date_attempt(
            **_transition_args(spec, expected_revision=1, now=NOW)
        )
    if case == "pause":
        return await store.pause_retryable(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            reason_code="collection_failed_before_write",
        )
    if case == "block":
        return await store.block_unknown(
            **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
            reason_code="collection_write_outcome_unknown",
        )
    return await store.confirm_date(
        **_transition_args(spec, expected_revision=2, now=FINISHED_AT),
        collection=collection,
    )


def _store_for_body(
    body: object,
) -> tuple[SupabaseKrCalendarCollectionJobStore, httpx.AsyncClient]:
    async def handler(_request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(200, content=b"null")
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SupabaseKrCalendarCollectionJobStore(_settings(), client=client), client


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "SUPABASE_URL": "http://127.0.0.1:54321",
            "SUPABASE_SECRET_KEY": SecretStr("test-secret"),
        }
    )


def _spec(
    *,
    job_id: str = JOB_ID,
    end_date: date = START_DATE,
) -> KrCalendarCollectionJobSpecV1:
    return KrCalendarCollectionJobSpecV1(
        job_id=job_id,
        provider="toss",
        market="KR",
        start_date=START_DATE,
        end_date=end_date,
        trigger="manual",
    )


def _collection(
    *,
    observed_at: datetime = NOW,
    session_date: date = START_DATE,
) -> CollectedKrDailySessionObservationV1:
    next_date = session_date + timedelta(days=1)
    session = PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=session_date,
        is_open=True,
        regular_start_at=datetime.combine(
            session_date,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=9),
        regular_end_at=datetime.combine(
            session_date,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=15, minutes=30),
        next_business_date=next_date,
        next_regular_start_at=datetime.combine(
            next_date,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=9),
        next_regular_end_at=datetime.combine(
            next_date,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=15, minutes=30),
        observed_at=observed_at,
        provider_contract_sha256=CONTRACT_SHA256,
    )
    receipt = CalendarObservationWriteReceipt(
        status="stored",
        calendar_idempotency_key=session.idempotency_key,
        canonical_evidence_sha256=session.canonical_evidence_sha256,
        revision=1,
        revision_inserted=True,
        occurrence_id=OCCURRENCE_ID,
        occurrence_inserted=True,
        observed_at=session.observed_at,
    )
    return CollectedKrDailySessionObservationV1(session=session, receipt=receipt)


def _ready(spec: KrCalendarCollectionJobSpecV1) -> KrCalendarCollectionJobSnapshotV1:
    return KrCalendarCollectionJobSnapshotV1(
        spec=spec,
        revision=1,
        state="ready",
        checkpoints=(),
        active_attempt=None,
        state_reason=None,
        terminal_manifest_sha256=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _active(
    spec: KrCalendarCollectionJobSpecV1,
    *,
    attempt_id: str = ATTEMPT_ID,
) -> KrCalendarCollectionJobSnapshotV1:
    attempt = KrCalendarCollectionDateAttemptV1(
        attempt_id=attempt_id,
        holder_id=HOLDER_ID,
        target_date=START_DATE,
        fencing_revision=2,
        begun_at=NOW,
    )
    return KrCalendarCollectionJobSnapshotV1(
        spec=spec,
        revision=2,
        state="collecting",
        checkpoints=(),
        active_attempt=attempt,
        state_reason=None,
        terminal_manifest_sha256=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _paused(
    spec: KrCalendarCollectionJobSpecV1,
    *,
    updated_at: datetime = FINISHED_AT,
) -> KrCalendarCollectionJobSnapshotV1:
    return KrCalendarCollectionJobSnapshotV1(
        spec=spec,
        revision=3,
        state="paused_retryable",
        checkpoints=(),
        active_attempt=None,
        state_reason="collection_failed_before_write",
        terminal_manifest_sha256=None,
        created_at=NOW,
        updated_at=updated_at,
    )


def _blocked(
    spec: KrCalendarCollectionJobSpecV1,
    *,
    attempt_id: str = ATTEMPT_ID,
) -> KrCalendarCollectionJobSnapshotV1:
    active = _active(spec, attempt_id=attempt_id).active_attempt
    assert active is not None
    return KrCalendarCollectionJobSnapshotV1(
        spec=spec,
        revision=3,
        state="blocked_unknown",
        checkpoints=(),
        active_attempt=active,
        state_reason="collection_write_outcome_unknown",
        terminal_manifest_sha256=None,
        created_at=NOW,
        updated_at=FINISHED_AT,
    )


def _completed(
    spec: KrCalendarCollectionJobSpecV1,
    *,
    collection: CollectedKrDailySessionObservationV1,
    attempt_id: str = ATTEMPT_ID,
) -> KrCalendarCollectionJobSnapshotV1:
    checkpoint = KrCalendarCollectionDateCheckpointV1(
        attempt_id=attempt_id,
        holder_id=HOLDER_ID,
        target_date=START_DATE,
        fencing_revision=2,
        begun_at=NOW,
        collection=collection,
        confirmed_at=FINISHED_AT,
    )
    checkpoints = (checkpoint,)
    return KrCalendarCollectionJobSnapshotV1(
        spec=spec,
        revision=3,
        state="completed",
        checkpoints=checkpoints,
        active_attempt=None,
        state_reason=None,
        terminal_manifest_sha256=kr_calendar_collection_job_manifest_sha256(
            spec,
            checkpoints,
        ),
        created_at=NOW,
        updated_at=FINISHED_AT,
    )


def _transition_args(
    spec: KrCalendarCollectionJobSpecV1,
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
        "target_date": START_DATE,
        "now": now,
    }


def _rpc_response(snapshot: KrCalendarCollectionJobSnapshotV1) -> list[object]:
    return [{"snapshot": _snapshot_payload(snapshot)}]


def _inspection_rpc_response(
    snapshot: KrCalendarCollectionJobSnapshotV1,
) -> list[object]:
    return [{"job_found": True, "snapshot": _snapshot_payload(snapshot)}]


def _snapshot_payload(snapshot: KrCalendarCollectionJobSnapshotV1) -> dict[str, object]:
    return {
        "schema_version": KR_CALENDAR_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION,
        "spec_sha256": snapshot.spec.spec_sha256,
        "spec": _spec_payload(snapshot.spec),
        "revision": snapshot.revision,
        "state": snapshot.state,
        "checkpoints": [
            {
                "attempt_id": checkpoint.attempt_id,
                "holder_id": checkpoint.holder_id,
                "target_date": checkpoint.target_date.isoformat(),
                "fencing_revision": checkpoint.fencing_revision,
                "begun_at": _timestamp(checkpoint.begun_at),
                "session": checkpoint.collection.session.to_payload(),
                "receipt": _receipt_payload(checkpoint.collection.receipt),
                "confirmed_at": _timestamp(checkpoint.confirmed_at),
            }
            for checkpoint in snapshot.checkpoints
        ],
        "active_attempt": (
            None
            if snapshot.active_attempt is None
            else {
                "attempt_id": snapshot.active_attempt.attempt_id,
                "holder_id": snapshot.active_attempt.holder_id,
                "target_date": snapshot.active_attempt.target_date.isoformat(),
                "fencing_revision": snapshot.active_attempt.fencing_revision,
                "begun_at": _timestamp(snapshot.active_attempt.begun_at),
            }
        ),
        "state_reason": snapshot.state_reason,
        "terminal_manifest_sha256": snapshot.terminal_manifest_sha256,
        "created_at": _timestamp(snapshot.created_at),
        "updated_at": _timestamp(snapshot.updated_at),
        "automatic_retry_allowed": False,
    }


def _spec_payload(spec: KrCalendarCollectionJobSpecV1) -> dict[str, object]:
    return {
        "schema_version": spec.schema_version,
        "job_id": spec.job_id,
        "provider": spec.provider,
        "market": spec.market,
        "start_date": spec.start_date.isoformat(),
        "end_date": spec.end_date.isoformat(),
        "trigger": spec.trigger,
    }


def _receipt_payload(receipt: CalendarObservationWriteReceipt) -> dict[str, object]:
    return {
        "status": receipt.status,
        "calendar_idempotency_key": receipt.calendar_idempotency_key,
        "canonical_evidence_sha256": receipt.canonical_evidence_sha256,
        "revision": receipt.revision,
        "revision_inserted": receipt.revision_inserted,
        "occurrence_id": str(receipt.occurrence_id),
        "occurrence_inserted": receipt.occurrence_inserted,
        "observed_at": _timestamp(receipt.observed_at),
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _set_path(
    payload: dict[str, object],
    path: tuple[str | int, ...],
    value: object,
) -> None:
    current: object = payload
    for key in path[:-1]:
        if isinstance(key, int):
            current = cast(list[object], current)[key]
        else:
            current = cast(dict[str, object], current)[key]
    last = path[-1]
    if isinstance(last, int):
        cast(list[object], current)[last] = value
    else:
        cast(dict[str, object], current)[last] = value

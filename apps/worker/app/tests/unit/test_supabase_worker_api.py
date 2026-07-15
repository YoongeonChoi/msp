from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_worker_api import (
    WORKER_API_RPC_ALLOWLIST,
    SupabaseWorkerApi,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import (
    AccountingTransaction,
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    LedgerPosting,
    WorkerLease,
)


def test_worker_api_is_disabled_by_default() -> None:
    settings = Settings(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )

    with pytest.raises(ExecutionInvariantError, match="worker_api_is_not_enabled"):
        SupabaseWorkerApi(settings, release_sha="a" * 40)


def test_worker_api_allowlist_matches_final_cutover_contract() -> None:
    assert {
        "acquire_worker_lease",
        "renew_worker_lease",
        "release_worker_lease",
        "record_worker_heartbeat",
        "reserve_order_intent",
        "mark_dispatch_started",
        "record_execution_observation",
        "claim_delivery_outbox",
        "complete_outbox_delivery",
        "fail_outbox_delivery",
        "acknowledge_operation_command",
        "claim_execution_reconciliation_batch",
        "fail_reserved_intent_pre_dispatch",
        "expire_paper_intent_remainder",
        "load_paper_execution_checkpoint",
        "complete_execution_reconciliation",
        "claim_operation_command_batch",
        "claim_cash_settlement_batch",
        "complete_cash_settlement",
        "fail_cash_settlement_attempt",
        "list_unknown_resolution_v2",
        "claim_unknown_resolution_v2",
        "apply_unknown_resolution_v2",
    } == WORKER_API_RPC_ALLOWLIST


async def test_worker_api_claims_outbox_with_attempt_lease_token() -> None:
    seen: list[httpx.Request] = []
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox_id = str(uuid4())
    aggregate_id = str(uuid4())
    lease_token = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "outbox_id": outbox_id,
                    "dedupe_key": f"incident:{aggregate_id}",
                    "event_type": "incident_alert",
                    "payload_version": 1,
                    "aggregate_type": "incident",
                    "aggregate_id": aggregate_id,
                    "payload": {"summary_code": "paper_execution_failed"},
                    "destination_type": "ops_webhook",
                    "attempt_count": 1,
                    "lease_token": lease_token,
                    "lease_expires_at": (now + timedelta(seconds=30)).isoformat(),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        claimed = await adapter.claim_delivery_outbox(
            worker_id="worker-a",
            now=now,
            limit=10,
            lease_ttl=timedelta(seconds=30),
        )

    assert len(claimed) == 1
    assert claimed[0].lease_token == lease_token
    assert seen[0].url.path == "/rest/v1/rpc/claim_delivery_outbox"
    assert json.loads(seen[0].content) == {
        "p_worker_id": "worker-a",
        "p_now": now.isoformat(),
        "p_limit": 10,
        "p_lease_seconds": 30,
    }


async def test_worker_api_settles_outbox_with_attempt_lease_token() -> None:
    seen: list[httpx.Request] = []
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox_id = str(uuid4())
    lease_token = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/complete_outbox_delivery"):
            return httpx.Response(
                200,
                json=[
                    {
                        "outbox_id": outbox_id,
                        "status": "delivered",
                        "delivered_at": now.isoformat(),
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "outbox_id": outbox_id,
                    "status": "pending",
                    "available_at": (now + timedelta(seconds=30)).isoformat(),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        await adapter.complete_outbox_delivery(
            outbox_id=outbox_id,
            worker_id="worker-a",
            lease_token=lease_token,
            now=now,
            external_receipt_id="receiver-42",
            external_receipt_sha256="b" * 64,
        )
        await adapter.fail_outbox_delivery(
            outbox_id=outbox_id,
            worker_id="worker-a",
            lease_token=lease_token,
            now=now,
            error_code="destination_timeouterror",
            retry_after=timedelta(seconds=30),
        )

    assert [request.url.path for request in seen] == [
        "/rest/v1/rpc/complete_outbox_delivery",
        "/rest/v1/rpc/fail_outbox_delivery",
    ]
    assert json.loads(seen[0].content) == {
        "p_outbox_id": outbox_id,
        "p_worker_id": "worker-a",
        "p_lease_token": lease_token,
        "p_now": now.isoformat(),
        "p_external_receipt_id": "receiver-42",
        "p_external_receipt_sha256": "b" * 64,
    }
    assert json.loads(seen[1].content) == {
        "p_outbox_id": outbox_id,
        "p_worker_id": "worker-a",
        "p_lease_token": lease_token,
        "p_now": now.isoformat(),
        "p_error_code": "destination_timeouterror",
        "p_retry_after_seconds": 30,
    }


@pytest.mark.parametrize("release_sha", ["a" * 7, "A" * 40, "a" * 41])
def test_worker_api_rejects_release_sha_outside_exact_database_contract(
    release_sha: str,
) -> None:
    with pytest.raises(ExecutionInvariantError, match="release_sha_is_missing"):
        SupabaseWorkerApi(_enabled_settings(), release_sha=release_sha)


async def test_worker_api_uses_private_schema_and_exact_lease_rpc_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "account_id": "account-a",
                    "holder_id": "worker-a",
                    "fencing_token": 7,
                    "acquired_at": "2026-07-14T09:00:00+00:00",
                    "expires_at": "2026-07-14T09:01:00+00:00",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        lease = await adapter.acquire_worker_lease(
            account_id="account-a",
            holder_id="worker-a",
            now=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            ttl=timedelta(seconds=60),
        )

    assert lease.fencing_token == 7
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/rest/v1/rpc/acquire_worker_lease"
    assert request.headers["content-profile"] == "worker_api"
    assert request.headers["accept-profile"] == "worker_api"
    assert json.loads(request.content) == {
        "p_account_id": "account-a",
        "p_holder_id": "worker-a",
        "p_now": "2026-07-14T09:00:00+00:00",
        "p_ttl_seconds": 60,
        "p_release_sha": "a" * 40,
    }


async def test_worker_api_rejects_malformed_rpc_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="response_fields_are_invalid"):
            await adapter.acquire_worker_lease(
                account_id="account-a",
                holder_id="worker-a",
                now=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
                ttl=timedelta(seconds=60),
            )


@pytest.mark.parametrize("idempotent", [False, True])
async def test_worker_api_releases_lease_with_identity_cas(idempotent: bool) -> None:
    seen_payloads: list[object] = []
    released_at = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    lease = WorkerLease(
        account_id="account-a",
        holder_id="worker-a",
        fencing_token=7,
        acquired_at=released_at - timedelta(seconds=30),
        expires_at=released_at + timedelta(seconds=30),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "account_id": lease.account_id,
                    "holder_id": lease.holder_id,
                    "fencing_token": lease.fencing_token,
                    "released_at": released_at.isoformat(),
                    "idempotent": idempotent,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        released = await adapter.release_worker_lease(lease, now=released_at)

    assert released.idempotent is idempotent
    assert released.released_at == released_at
    assert seen_payloads == [
        {
            "p_account_id": "account-a",
            "p_holder_id": "worker-a",
            "p_fencing_token": 7,
            "p_now": released_at.isoformat(),
            "p_release_sha": "a" * 40,
        }
    ]


async def test_worker_api_rejects_released_lease_identity_mismatch() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    lease = WorkerLease(
        account_id="account-a",
        holder_id="worker-a",
        fencing_token=7,
        acquired_at=now - timedelta(seconds=30),
        expires_at=now + timedelta(seconds=30),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "account_id": "account-b",
                    "holder_id": lease.holder_id,
                    "fencing_token": lease.fencing_token,
                    "released_at": now.isoformat(),
                    "idempotent": False,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="released_lease_identity_mismatch"):
            await adapter.release_worker_lease(lease, now=now)


async def test_worker_api_fails_closed_when_release_lease_cas_is_stale() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    lease = WorkerLease(
        account_id="account-a",
        holder_id="worker-a",
        fencing_token=7,
        acquired_at=now - timedelta(seconds=30),
        expires_at=now + timedelta(seconds=30),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "40001"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="rpc_failed"):
            await adapter.release_worker_lease(lease, now=now)


async def test_worker_api_records_heartbeat_with_pinned_release_sha() -> None:
    seen_payloads: list[object] = []
    worker_id = "00000000-0000-4000-8000-000000000001"
    heartbeat_id = "00000000-0000-4000-8000-000000000002"
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[{"heartbeat_id": heartbeat_id, "created_at": now.isoformat()}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        heartbeat = await adapter.record_worker_heartbeat(
            worker_id=worker_id,
            status="warning",
            details={"checkpoint": "operations_started"},
            now=now,
        )

    assert heartbeat.heartbeat_id == heartbeat_id
    assert seen_payloads == [
        {
            "p_worker_id": worker_id,
            "p_status": "warning",
            "p_details": {
                "checkpoint": "operations_started",
                "release_sha": "a" * 40,
            },
            "p_now": now.isoformat(),
            "p_release_sha": "a" * 40,
        }
    ]


async def test_worker_api_preserves_error_heartbeat_status_and_payload() -> None:
    seen_payloads: list[object] = []
    worker_id = "00000000-0000-4000-8000-000000000001"
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "heartbeat_id": "00000000-0000-4000-8000-000000000003",
                    "created_at": now.isoformat(),
                }
            ],
        )

    details: JsonObject = {
        "checkpoint": "operations_exception",
        "completed_at": now.isoformat(),
        "error_type": "RuntimeError",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        await adapter.record_worker_heartbeat(
            worker_id=worker_id,
            status="error",
            details=details,
            now=now,
        )

    assert seen_payloads == [
        {
            "p_worker_id": worker_id,
            "p_status": "error",
            "p_details": details | {"release_sha": "a" * 40},
            "p_now": now.isoformat(),
            "p_release_sha": "a" * 40,
        }
    ]


async def test_worker_api_rejects_heartbeat_release_sha_mismatch_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="heartbeat_release_sha_mismatch"):
            await adapter.record_worker_heartbeat(
                worker_id="00000000-0000-4000-8000-000000000001",
                status="warning",
                details={"release_sha": "b" * 40},
                now=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            )

    assert calls == 0


async def test_worker_api_rejects_ok_heartbeat_without_completion_evidence() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="heartbeat_checkpoint_invalid"):
            await adapter.record_worker_heartbeat(
                worker_id="00000000-0000-4000-8000-000000000001",
                status="ok",
                details={"checkpoint": "operations_started"},
                now=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            )

    assert calls == 0


async def test_worker_api_accepts_complete_independent_scheduler_heartbeat() -> None:
    seen_payloads: list[object] = []
    worker_id = "00000000-0000-4000-8000-000000000001"
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    stage_time = (now - timedelta(seconds=1)).isoformat()
    details: JsonObject = {
        "component": "operations_v2",
        "checkpoint": "independent_scheduler_running",
        "completed_at": now.isoformat(),
        "stage_last_completed_at": {
            "commands": stage_time,
            "execution": stage_time,
            "settlement": stage_time,
            "reconciliation": stage_time,
            "outbox": stage_time,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "heartbeat_id": "00000000-0000-4000-8000-000000000004",
                    "created_at": now.isoformat(),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        await adapter.record_worker_heartbeat(
            worker_id=worker_id,
            status="ok",
            details=details,
            now=now,
        )

    assert seen_payloads == [
        {
            "p_worker_id": worker_id,
            "p_status": "ok",
            "p_details": details | {"release_sha": "a" * 40},
            "p_now": now.isoformat(),
            "p_release_sha": "a" * 40,
        }
    ]


async def test_worker_api_rejects_incomplete_scheduler_heartbeat_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(
            ExecutionInvariantError,
            match="scheduler_heartbeat_stages_invalid",
        ):
            await adapter.record_worker_heartbeat(
                worker_id="00000000-0000-4000-8000-000000000001",
                status="ok",
                details={
                    "component": "operations_v2",
                    "checkpoint": "independent_scheduler_running",
                    "completed_at": now.isoformat(),
                    "stage_last_completed_at": {
                        "commands": now.isoformat(),
                        "execution": now.isoformat(),
                        "settlement": now.isoformat(),
                        "reconciliation": now.isoformat(),
                        "outbox": None,
                    },
                },
                now=now,
            )

    assert calls == 0


async def test_worker_api_mark_dispatch_uses_request_hash_and_client_key() -> None:
    seen_payloads: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "attempt_id": "00000000-0000-0000-0000-000000000001",
                    "prepared_at": "2026-07-14T09:00:30+00:00",
                    "reason_code": None,
                }
            ],
        )

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _contract_intent(now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        await adapter.mark_dispatch_started(intent, now=now)

    assert len(seen_payloads) == 1
    payload = seen_payloads[0]
    assert isinstance(payload, dict)
    assert payload["p_client_order_key"] == intent.semantic_key
    request_sha256 = payload["p_request_sha256"]
    assert isinstance(request_sha256, str)
    assert len(request_sha256) == 64


async def test_worker_api_reserve_payload_matches_durable_intent_contract() -> None:
    seen_payloads: list[object] = []
    reservation_id = "00000000-0000-0000-0000-000000000002"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_payloads.append(payload)
        return httpx.Response(
            200,
            json=[
                {
                    "reserved": True,
                    "intent_id": payload["p_intent_id"],
                    "reservation_id": reservation_id,
                    "reason_code": "reserved",
                }
            ],
        )

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _contract_intent(now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        reservation = await adapter.reserve_order_intent(intent)

    assert reservation.state == "created"
    assert reservation.intent_id == intent.id

    payload = seen_payloads[0]
    assert isinstance(payload, dict)
    assert payload == {
        "p_intent_id": intent.id,
        "p_semantic_key": intent.semantic_key,
        "p_account_id": intent.account_id,
        "p_environment": intent.environment,
        "p_strategy_version_id": intent.strategy_version_id,
        "p_decision_id": intent.decision_id,
        "p_risk_result_id": intent.risk_result_id,
        "p_decision_feature_sha256": intent.decision_feature_sha256,
        "p_risk_allowed": True,
        "p_risk_reason_codes": [],
        "p_risk_evaluated_at": intent.risk_evaluated_at.isoformat(),
        "p_risk_expires_at": intent.risk_expires_at.isoformat(),
        "p_symbol": intent.symbol,
        "p_side": intent.side,
        "p_quantity": intent.quantity,
        "p_limit_price_krw": intent.limit_price_krw,
        "p_decision_at": intent.decision_at.isoformat(),
        "p_signal_valid_from": intent.signal_valid_from.isoformat(),
        "p_signal_valid_until": intent.signal_valid_until.isoformat(),
        "p_execution_policy_version": intent.execution_policy_version,
        "p_cost_schedule_version": intent.cost_schedule_version,
        "p_cost_schedule_evidence_sha256": intent.cost_schedule_evidence_sha256,
        "p_cash_commitment_krw": intent.cash_commitment_krw,
        "p_eligible_at": intent.eligible_at.isoformat(),
        "p_expires_at": intent.expires_at.isoformat(),
        "p_gate_epoch": intent.gate_epoch,
        "p_holder_id": intent.lease_holder_id,
        "p_fencing_token": intent.lease_fencing_token,
        "p_release_sha": "a" * 40,
    }


async def test_worker_api_preserves_same_intent_retry_reservation_state() -> None:
    reservation_id = "00000000-0000-0000-0000-000000000002"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {
                    "reserved": False,
                    "intent_id": payload["p_intent_id"],
                    "reservation_id": reservation_id,
                    "reason_code": "duplicate_semantic_intent",
                }
            ],
        )

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _contract_intent(now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        reservation = await adapter.reserve_order_intent(intent)

    assert reservation.state == "existing_replay"
    assert reservation.intent_id == intent.id


async def test_worker_api_accepts_canonical_intent_for_semantic_duplicate() -> None:
    canonical_intent_id = "00000000-0000-4000-8000-000000000099"
    reservation_id = "00000000-0000-4000-8000-000000000002"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "reserved": False,
                    "intent_id": canonical_intent_id,
                    "reservation_id": reservation_id,
                    "reason_code": "duplicate_semantic_intent",
                }
            ],
        )

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _contract_intent(now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        reservation = await adapter.reserve_order_intent(intent)

    assert reservation.state == "semantic_duplicate"
    assert reservation.intent_id == canonical_intent_id


async def test_worker_api_sends_accounting_with_fill_observation_atomically() -> None:
    seen_payloads: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "observation_id": "00000000-0000-0000-0000-000000000003",
                    "inserted": True,
                    "quarantined": False,
                    "reason_code": "recorded",
                }
            ],
        )

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _contract_intent(now)
    observed_at = intent.eligible_at
    observation = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=1,
        status="filled",
        observed_at=observed_at,
        provider_order_id="contract-00000001",
        provider_execution_id="contract-00000001-execution-1",
        cumulative_quantity=intent.quantity,
        cumulative_gross_krw=intent.quantity * intent.limit_price_krw,
        cumulative_commission_krw=3,
        cumulative_tax_krw=0,
        last_fill_quantity=intent.quantity,
        last_fill_price_krw=intent.limit_price_krw,
        last_fill_settlement_date=observed_at.date() + timedelta(days=2),
    )
    transaction = AccountingTransaction(
        id="transaction-key",
        intent_id=intent.id,
        observation_sequence=1,
        posted_at=observed_at,
        postings=(
            LedgerPosting("POSITION_COST", debit_krw=20_000),
            LedgerPosting("FEES", debit_krw=3),
            LedgerPosting("CASH", credit_krw=20_003),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        await adapter.record_execution_observation(
            intent,
            observation,
            accounting_transaction=transaction,
            intent_release_sha="a" * 40,
            now=observed_at,
        )

    payload = seen_payloads[0]
    assert isinstance(payload, dict)
    assert payload["p_accounting_postings"] == [
        {"account": "POSITION_COST", "debit_krw": 20_000, "credit_krw": 0},
        {"account": "FEES", "debit_krw": 3, "credit_krw": 0},
        {"account": "CASH", "debit_krw": 0, "credit_krw": 20_003},
    ]
    assert payload["p_provider_order_id"] == observation.provider_order_id
    assert payload["p_provider_execution_id"] == observation.provider_execution_id
    assert payload["p_provider_observation_sha256"] == (
        observation.provider_observation_sha256
    )
    assert observation.last_fill_settlement_date is not None
    assert payload["p_last_fill_settlement_date"] == (
        observation.last_fill_settlement_date.isoformat()
    )


async def test_worker_api_rejects_fill_without_accounting_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _contract_intent(now)
    observation = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=1,
        status="filled",
        observed_at=intent.eligible_at,
        provider_order_id="contract-00000001",
        provider_execution_id="contract-00000001-execution-1",
        cumulative_quantity=intent.quantity,
        cumulative_gross_krw=intent.quantity * intent.limit_price_krw,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        last_fill_quantity=intent.quantity,
        last_fill_price_krw=intent.limit_price_krw,
        last_fill_settlement_date=intent.eligible_at.date() + timedelta(days=2),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="requires_accounting_transaction"):
            await adapter.record_execution_observation(
                intent,
                observation,
                intent_release_sha="a" * 40,
                now=intent.eligible_at,
            )

    assert calls == 0


async def test_worker_api_rejects_cross_release_observation_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _paper_intent(now)
    observation = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=1,
        status="open",
        observed_at=intent.eligible_at,
        provider_order_id=f"paper:{intent.id}",
        provider_execution_id=None,
        cumulative_quantity=0,
        cumulative_gross_krw=0,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="b" * 40,
            client=client,
        )
        with pytest.raises(
            ExecutionInvariantError,
            match="observation_origin_release_mismatch",
        ):
            await adapter.record_execution_observation(
                intent,
                observation,
                intent_release_sha="a" * 40,
                now=intent.eligible_at,
            )

    assert calls == 0


async def test_worker_api_uses_exact_reconciliation_claim_and_completion_contract() -> None:
    seen: list[tuple[str, object]] = []
    intent_id = "00000000-0000-0000-0000-000000000010"
    account_id = "paper-primary"
    decision_id = "00000000-0000-0000-0000-000000000030"
    risk_result_id = "00000000-0000-0000-0000-000000000040"
    worker_id = "00000000-0000-4000-8000-000000000050"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((request.url.path, payload))
        if request.url.path.endswith("/claim_execution_reconciliation_batch"):
            return httpx.Response(
                200,
                json=[
                    {
                        "intent_id": intent_id,
                        "attempt_id": None,
                        "provider_order_id": None,
                        "latest_observation_id": None,
                        "latest_sequence": None,
                        "latest_status": None,
                        "latest_observed_at": None,
                        "latest_cumulative_quantity": None,
                        "latest_cumulative_gross_krw": None,
                        "latest_cumulative_commission_krw": None,
                        "latest_cumulative_tax_krw": None,
                        "observation_history_sha256": None,
                        "environment": "paper",
                        "account_id": account_id,
                        "symbol": "005930",
                        "side": "buy",
                        "quantity": 2,
                        "limit_price_krw": 10_000,
                        "eligible_at": "2026-07-14T09:00:00+00:00",
                        "expires_at": "2026-07-14T09:05:00+00:00",
                        "semantic_key_sha256": "a" * 64,
                        "decision_id": decision_id,
                        "risk_result_id": risk_result_id,
                        "execution_policy_version": "paper-minute-v1",
                        "cost_schedule_version": "fees-v1",
                        "risk_policy_sha256": "b" * 64,
                        "provider_contract_version": None,
                        "provider_openapi_sha256": None,
                        "position_cost_basis_method": None,
                        "position_quantity_snapshot": None,
                        "position_average_cost_krw": None,
                        "position_total_cost_krw": None,
                        "position_projection_version": None,
                        "position_cost_basis_sha256": None,
                        "lease_fencing_token": 7,
                        "reservation_fencing_token": 2,
                        "control_epoch": 3,
                        "reservation_control_epoch": 1,
                        "intent_release_sha": "a" * 40,
                        "lease_release_sha": "a" * 40,
                        "recovery_disposition": "same_release",
                        "priority": 10,
                        "next_reconcile_at": "2026-07-14T09:00:00+00:00",
                        "lease_expires_at": "2026-07-14T09:00:30+00:00",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "intent_id": intent_id,
                    "state": "pending",
                    "next_reconcile_at": "2026-07-14T09:01:00+00:00",
                }
            ],
        )

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        claims = await adapter.claim_execution_reconciliation_batch(
            worker_id=worker_id,
            now=now,
            limit=50,
            after_priority=None,
            after_intent_id=None,
            lease_ttl=timedelta(seconds=30),
        )
        completion = await adapter.complete_execution_reconciliation(
            intent_id=intent_id,
            worker_id=worker_id,
            release_sha="a" * 40,
            fencing_token=7,
            now=now,
            outcome="reschedule",
            next_reconcile_at=now + timedelta(minutes=1),
            reason_code="provider_order_still_open",
        )

    assert claims[0].cursor == (10, intent_id)
    assert claims[0].account_id == "paper-primary"
    assert claims[0].lease_fencing_token == 7
    assert claims[0].control_epoch == 3
    assert claims[0].intent_release_sha == "a" * 40
    assert claims[0].lease_release_sha == "a" * 40
    assert completion.state == "pending"
    assert seen == [
        (
            "/rest/v1/rpc/claim_execution_reconciliation_batch",
            {
                "p_worker_id": worker_id,
                "p_now": now.isoformat(),
                "p_limit": 50,
                "p_after_priority": None,
                "p_after_intent_id": None,
                "p_lease_seconds": 30,
                "p_release_sha": "a" * 40,
            },
        ),
        (
            "/rest/v1/rpc/complete_execution_reconciliation",
            {
                "p_intent_id": intent_id,
                "p_worker_id": worker_id,
                "p_release_sha": "a" * 40,
                "p_fencing_token": 7,
                "p_now": now.isoformat(),
                "p_outcome": "reschedule",
                "p_next_reconcile_at": (now + timedelta(minutes=1)).isoformat(),
                "p_reason_code": "provider_order_still_open",
            },
        ),
    ]


@pytest.mark.parametrize(
    ("release_sha", "fencing_token", "error"),
    [
        ("b" * 40, 7, "worker_api_release_sha_mismatch"),
        ("a" * 40, 0, "worker_api_reconciliation_fencing_token_is_invalid"),
        ("a" * 40, -1, "worker_api_reconciliation_fencing_token_is_invalid"),
        ("a" * 40, True, "worker_api_reconciliation_fencing_token_is_invalid"),
    ],
)
async def test_worker_api_rejects_invalid_reconciliation_completion_identity(
    release_sha: str,
    fencing_token: int,
    error: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid completion must fail before network")

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match=error):
            await adapter.complete_execution_reconciliation(
                intent_id="00000000-0000-4000-8000-000000000010",
                worker_id="00000000-0000-4000-8000-000000000001",
                release_sha=release_sha,
                fencing_token=fencing_token,
                now=now,
                outcome="reschedule",
                next_reconcile_at=now + timedelta(minutes=1),
                reason_code="provider_order_still_open",
            )


@pytest.mark.parametrize("account_id", ["", "   "])
async def test_worker_api_rejects_empty_reconciliation_account_slug(
    account_id: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "intent_id": "00000000-0000-0000-0000-000000000010",
                    "attempt_id": None,
                    "provider_order_id": None,
                    "latest_observation_id": None,
                    "latest_sequence": None,
                    "latest_status": None,
                    "latest_observed_at": None,
                    "latest_cumulative_quantity": None,
                    "latest_cumulative_gross_krw": None,
                    "latest_cumulative_commission_krw": None,
                    "latest_cumulative_tax_krw": None,
                    "observation_history_sha256": None,
                    "environment": "paper",
                    "account_id": account_id,
                    "symbol": "005930",
                    "side": "buy",
                    "quantity": 2,
                    "limit_price_krw": 10_000,
                    "eligible_at": "2026-07-14T09:00:00+00:00",
                    "expires_at": "2026-07-14T09:05:00+00:00",
                    "semantic_key_sha256": "a" * 64,
                    "decision_id": "00000000-0000-0000-0000-000000000030",
                    "risk_result_id": "00000000-0000-0000-0000-000000000040",
                    "execution_policy_version": "paper-minute-v1",
                    "cost_schedule_version": "fees-v1",
                    "risk_policy_sha256": "b" * 64,
                    "provider_contract_version": None,
                    "provider_openapi_sha256": None,
                    "position_cost_basis_method": None,
                    "position_quantity_snapshot": None,
                    "position_average_cost_krw": None,
                    "position_total_cost_krw": None,
                    "position_projection_version": None,
                    "position_cost_basis_sha256": None,
                    "lease_fencing_token": 7,
                    "reservation_fencing_token": 2,
                    "control_epoch": 3,
                    "reservation_control_epoch": 1,
                    "intent_release_sha": "a" * 40,
                    "lease_release_sha": "a" * 40,
                    "recovery_disposition": "same_release",
                    "priority": 10,
                    "next_reconcile_at": "2026-07-14T09:00:00+00:00",
                    "lease_expires_at": "2026-07-14T09:00:30+00:00",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="response_field_is_invalid"):
            await adapter.claim_execution_reconciliation_batch(
                worker_id="00000000-0000-4000-8000-000000000050",
                now=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
                limit=50,
                after_priority=None,
                after_intent_id=None,
                lease_ttl=timedelta(seconds=30),
            )


@pytest.mark.parametrize("idempotent", [False, True])
async def test_worker_api_fails_reserved_intent_pre_dispatch_atomically(
    idempotent: bool,
) -> None:
    seen_payloads: list[object] = []
    intent_id = "00000000-0000-4000-8000-000000000010"
    observation_id = "00000000-0000-4000-8000-000000000011"
    worker_id = "00000000-0000-4000-8000-000000000050"
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "intent_id": intent_id,
                    "observation_id": observation_id,
                    "state": "complete",
                    "reason_code": "worker_restart_before_dispatch",
                    "idempotent": idempotent,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        result = await adapter.fail_reserved_intent_pre_dispatch(
            intent_id=intent_id,
            worker_id=worker_id,
            fencing_token=7,
            control_epoch=3,
            release_sha="a" * 40,
            now=now,
            reason_code="worker_restart_before_dispatch",
        )

    assert result.observation_id == observation_id
    assert result.idempotent is idempotent
    assert seen_payloads == [
        {
            "p_intent_id": intent_id,
            "p_worker_id": worker_id,
            "p_fencing_token": 7,
            "p_control_epoch": 3,
            "p_release_sha": "a" * 40,
            "p_now": now.isoformat(),
            "p_reason_code": "worker_restart_before_dispatch",
        }
    ]


async def test_worker_api_loads_exact_fenced_paper_checkpoint() -> None:
    seen_payloads: list[object] = []
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    intent = _paper_intent(now)
    attempt_id = "00000000-0000-4000-8000-000000000012"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "intent_id": intent.id,
                    "attempt_id": attempt_id,
                    "provider_order_id": None,
                    "latest_sequence": None,
                    "latest_status": None,
                    "latest_observed_at": None,
                    "latest_cumulative_quantity": 0,
                    "latest_cumulative_gross_krw": 0,
                    "latest_cumulative_commission_krw": 0,
                    "latest_cumulative_tax_krw": 0,
                    "observation_history_sha256": None,
                    "expires_at": intent.expires_at.isoformat(),
                    "intent_release_sha": "a" * 40,
                    "lease_release_sha": "a" * 40,
                    "position_cost_basis_method": None,
                    "position_quantity_snapshot": None,
                    "position_total_cost_krw": None,
                    "position_cost_basis_sha256": None,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        checkpoint = await adapter.load_paper_execution_checkpoint(intent, now=now)

    assert checkpoint.attempt_id == attempt_id
    assert checkpoint.latest_sequence is None
    assert seen_payloads == [
        {
            "p_intent_id": intent.id,
            "p_account_id": intent.account_id,
            "p_holder_id": intent.lease_holder_id,
            "p_fencing_token": intent.lease_fencing_token,
            "p_control_epoch": intent.gate_epoch,
            "p_release_sha": "a" * 40,
            "p_now": now.isoformat(),
        }
    ]


@pytest.mark.parametrize("idempotent", [False, True])
async def test_worker_api_atomically_expires_paper_remainder(
    idempotent: bool,
) -> None:
    seen_payloads: list[object] = []
    intent_id = "00000000-0000-4000-8000-000000000010"
    observation_id = "00000000-0000-4000-8000-000000000011"
    worker_id = "00000000-0000-4000-8000-000000000050"
    now = datetime(2026, 7, 14, 9, 5, tzinfo=UTC)
    reason_code = "paper_day_limit_remainder_expired_after_recovery"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "intent_id": intent_id,
                    "observation_id": observation_id,
                    "sequence": 2,
                    "state": "complete",
                    "reason_code": reason_code,
                    "idempotent": idempotent,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        result = await adapter.expire_paper_intent_remainder(
            intent_id=intent_id,
            worker_id=worker_id,
            fencing_token=7,
            control_epoch=3,
            release_sha="a" * 40,
            now=now,
            reason_code=reason_code,
        )

    assert result.sequence == 2
    assert result.idempotent is idempotent
    assert seen_payloads == [
        {
            "p_intent_id": intent_id,
            "p_worker_id": worker_id,
            "p_fencing_token": 7,
            "p_control_epoch": 3,
            "p_release_sha": "a" * 40,
            "p_now": now.isoformat(),
            "p_reason_code": reason_code,
        }
    ]


async def test_worker_api_claims_operation_commands_with_release_pin() -> None:
    seen_payloads: list[object] = []
    holder_id = "00000000-0000-4000-8000-000000000001"
    command_id = "00000000-0000-4000-8000-000000000002"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "command_id": command_id,
                    "command_type": "account_opening",
                    "environment": "paper",
                    "account_id": "paper-primary",
                    "requested_change": {"expected_state_version": 3},
                    "requested_at": "2026-07-14T08:59:00+00:00",
                    "expires_at": "2026-07-14T09:05:00+00:00",
                    "revision": 4,
                    "claimed_at": "2026-07-14T09:00:00+00:00",
                    "claim_expires_at": "2026-07-14T09:00:30+00:00",
                }
            ],
        )

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _enabled_settings(),
            release_sha="a" * 40,
            client=client,
        )
        commands = await adapter.claim_operation_command_batch(
            holder_id=holder_id,
            now=now,
            limit=25,
        )

    assert commands[0].command_type == "account_opening"
    assert commands[0].claim_expires_at == now + timedelta(seconds=30)
    assert seen_payloads == [
        {
            "p_holder_id": holder_id,
            "p_release_sha": "a" * 40,
            "p_now": now.isoformat(),
            "p_limit": 25,
        }
    ]


def _enabled_settings() -> Settings:
    return Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )


def _contract_intent(now: datetime) -> ExecutionIntent:
    expires_at = now.replace(hour=15, minute=30, second=0)
    schedule = ExecutionCostSchedule(
        version="fees-v1",
        effective_from=now - timedelta(days=1),
        effective_until=expires_at + timedelta(days=1),
        evidence_sha256="b" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )
    return ExecutionIntent.create(
        account_id="account-a",
        environment="contract_test",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="e" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=now + timedelta(hours=1),
        strategy_version_id="strategy-v1",
        symbol="005930",
        side="buy",
        quantity=2,
        limit_price_krw=10_000,
        decision_at=now,
        signal_valid_from=now - timedelta(minutes=1),
        signal_valid_until=now + timedelta(hours=7),
        execution_policy_version="limit-day-v1",
        cost_schedule=schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id="worker-a",
        lease_fencing_token=1,
    )


def _paper_intent(now: datetime) -> ExecutionIntent:
    expires_at = now.replace(hour=15, minute=30, second=0)
    schedule = ExecutionCostSchedule(
        version="fees-v1",
        effective_from=now - timedelta(days=1),
        effective_until=expires_at + timedelta(days=1),
        evidence_sha256="a" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )
    return ExecutionIntent.create(
        account_id="paper-primary",
        environment="paper",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="e" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=now + timedelta(hours=8),
        strategy_version_id="strategy-v1",
        symbol="005930",
        side="buy",
        quantity=2,
        limit_price_krw=10_000,
        decision_at=now,
        signal_valid_from=now - timedelta(minutes=1),
        signal_valid_until=now + timedelta(hours=7),
        execution_policy_version="paper-minute-v1",
        cost_schedule=schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id="00000000-0000-4000-8000-000000000001",
        lease_fencing_token=7,
    )

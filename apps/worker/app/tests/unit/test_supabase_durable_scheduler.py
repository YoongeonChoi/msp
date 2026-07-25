from __future__ import annotations

import asyncio
import gzip
import json
import traceback
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_durable_scheduler import (
    DURABLE_SCHEDULER_DETERMINISTIC_REJECTION_STATUSES,
    DURABLE_SCHEDULER_MAX_RPC_RESPONSE_BYTES,
    DURABLE_SCHEDULER_RPC_ALLOWLIST,
    SupabaseDurableScheduler,
)
from app.adapters.persistence.supabase_durable_scheduler_wire import (
    DurableSchedulerWireCodec,
)
from app.application.ports.durable_scheduler_port import (
    DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS,
    SchedulerMutationOutcomeUnknownError,
    SchedulerTransitionRejectedError,
)
from app.config import Settings
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    ScheduledJobClaimV1,
    ScheduledJobDefinitionV1,
    ScheduledJobLeaseV1,
    ScheduledJobRunV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerReplayAssessmentV1,
)

NOW = datetime(2026, 7, 24, 3, 0, tzinfo=UTC)
RUN_ID = "11111111-1111-4111-8111-111111111111"
NEW_RUN_ID = "22222222-2222-4222-8222-222222222222"
LEASE_TOKEN = "33333333-3333-4333-8333-333333333333"
HOLDER_ID = "44444444-4444-4444-8444-444444444444"
DEFINITION_ID = "55555555-5555-4555-8555-555555555555"
REPLAY_REQUEST_ID = "66666666-6666-4666-8666-666666666666"
RELEASE_SHA = "a" * 40
FAILURE_SHA = "b" * 64


async def test_adapter_uses_exact_definition_rpc_without_caller_clock() -> None:
    definition = _definition()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rest/v1/rpc/ensure_scheduler_job_definition")
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["content-profile"] == "worker_api"
        assert request.headers["accept-encoding"] == "identity"
        payload = _request_payload(request)
        assert set(payload) == {
            "p_account_id",
            "p_holder_id",
            "p_outer_fencing_token",
            "p_release_sha",
            "p_job_key",
            "p_definition_sha256",
            "p_interval_seconds",
            "p_lease_ttl_seconds",
            "p_max_attempts",
            "p_retry_base_seconds",
            "p_retry_max_seconds",
            "p_max_manual_replays",
            "p_enabled",
        }
        assert "p_now" not in payload
        assert payload["p_definition_sha256"] == definition.definition_sha256
        return httpx.Response(
            200,
            json=[
                {
                    "definition_id": DEFINITION_ID,
                    "account_id": "paper-primary",
                    "job_key": definition.job_key,
                    "definition_sha256": definition.definition_sha256,
                    "revision": 1,
                    "next_due_at": _timestamp(NOW - timedelta(minutes=1)),
                    "observed_at": _timestamp(NOW),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        receipt = await adapter.ensure_job_definition(
            definition,
            outer_lease=_outer_lease(),
        )
    finally:
        await client.aclose()

    assert receipt.definition_id == DEFINITION_ID
    assert receipt.next_due_at < receipt.observed_at


async def test_adapter_decodes_exact_quiescent_convergence_contract() -> None:
    definition = _definition()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/rest/v1/rpc/converge_scheduler_job_definition"
        )
        payload = _request_payload(request)
        assert payload["p_definition_sha256"] == definition.definition_sha256
        assert "p_now" not in payload
        return httpx.Response(200, json=_convergence_response(definition))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        receipt = await adapter.converge_job_definition(
            definition,
            outer_lease=_outer_lease(),
        )
    finally:
        await client.aclose()

    assert isinstance(receipt, SchedulerDefinitionConvergenceReceiptV1)
    assert receipt.status == "converged"
    assert receipt.definition.definition == definition
    assert receipt.claim is None


async def test_adapter_decodes_outer_renewal_required_convergence_wait() -> None:
    definition = _definition()
    response = _convergence_response(definition)
    row = cast(dict[str, Any], response[0])
    row.update(
        {
            "status": "wait",
            "active_run_id": RUN_ID,
            "next_eligible_at": _timestamp(NOW + timedelta(seconds=10)),
            "reason_code": "scheduler_outer_lease_renewal_required",
        }
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        receipt = await adapter.converge_job_definition(
            definition,
            outer_lease=_outer_lease(),
        )
    finally:
        await client.aclose()

    assert receipt.status == "wait"
    assert receipt.reason_code == "scheduler_outer_lease_renewal_required"


async def test_adapter_parses_exact_nested_claim_and_binds_outer_actor() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rest/v1/rpc/claim_due_scheduler_job")
        assert _request_payload(request) == {
            "p_account_id": "paper-primary",
            "p_holder_id": HOLDER_ID,
            "p_outer_fencing_token": 7,
            "p_release_sha": RELEASE_SHA,
        }
        return httpx.Response(200, json=_claim_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        claim_receipt = await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()

    claim = claim_receipt.claim
    assert isinstance(claim, ScheduledJobClaimV1)
    assert claim.run.run_id == RUN_ID
    assert claim.lease.lease_token == LEASE_TOKEN
    assert claim.observed_at == NOW
    assert claim_receipt.observed_at == NOW


@pytest.mark.parametrize(
    "mutation",
    [
        "lease_time_differs_from_observed",
        "run_update_differs_from_observed",
    ],
)
async def test_adapter_rejects_claim_time_or_outer_lease_inconsistency(
    mutation: str,
) -> None:
    response = _claim_response()
    claim_row = cast(dict[str, Any], cast(dict[str, Any], response[0])["claim"])
    run_row = cast(dict[str, Any], claim_row["run"])
    lease_row = cast(dict[str, Any], claim_row["lease"])
    if mutation == "lease_time_differs_from_observed":
        lease_row["leased_at"] = _timestamp(NOW - timedelta(microseconds=1))
    else:
        run_row["updated_at"] = _timestamp(NOW - timedelta(microseconds=1))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()


async def test_adapter_rejects_claim_observed_before_outer_acquisition() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_claim_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(
                outer_lease=_outer_lease(
                    acquired_at=NOW + timedelta(seconds=1),
                    expires_at=NOW + timedelta(seconds=30),
                )
            )
    finally:
        await client.aclose()


async def test_adapter_rejects_disabled_definition_in_claim_response() -> None:
    response = _claim_response()
    claim_row = cast(dict[str, Any], cast(dict[str, Any], response[0])["claim"])
    definition_row = cast(dict[str, Any], claim_row["definition"])
    run_row = cast(dict[str, Any], claim_row["run"])
    disabled_definition = replace(_definition(), enabled=False)
    definition_row.update(
        {
            **disabled_definition.to_payload(),
            "definition_sha256": disabled_definition.definition_sha256,
        }
    )
    run_row["definition_sha256"] = disabled_definition.definition_sha256

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    decoded_receipt = DurableSchedulerWireCodec().decode_claim(response)
    decoded_claim = decoded_receipt.claim
    assert decoded_claim is not None
    assert decoded_claim.definition.enabled is False
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()


async def test_adapter_rejects_unsafe_definition_before_rpc() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    unsafe = replace(_definition(), max_attempts=4)
    try:
        with pytest.raises(
            SchedulerInvariantError,
            match="scheduler_definition_budget_is_unsafe",
        ):
            await adapter.ensure_job_definition(
                unsafe,
                outer_lease=_outer_lease(),
            )
    finally:
        await client.aclose()

    assert calls == 0


async def test_adapter_rejects_unsafe_claim_definition_as_unknown_outcome() -> None:
    response = _claim_response()
    claim_row = cast(dict[str, Any], cast(dict[str, Any], response[0])["claim"])
    definition_row = cast(dict[str, Any], claim_row["definition"])
    run_row = cast(dict[str, Any], claim_row["run"])
    unsafe = replace(_definition(), max_attempts=4)
    definition_row.update(
        {
            **unsafe.to_payload(),
            "definition_sha256": unsafe.definition_sha256,
        }
    )
    run_row["definition_sha256"] = unsafe.definition_sha256

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()


async def test_adapter_complete_and_fail_use_exact_db_clock_owned_contracts() -> None:
    definition = _definition()
    result_sha = "c" * 64
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        payload = _request_payload(request)
        assert "p_now" not in payload
        assert "p_retry_after" not in payload
        if request.url.path.endswith("/complete_scheduler_job_run"):
            assert set(payload) == {
                "p_account_id",
                "p_holder_id",
                "p_outer_fencing_token",
                "p_release_sha",
                "p_run_id",
                "p_expected_run_revision",
                "p_definition_sha256",
                "p_lease_token",
                "p_result_sha256",
            }
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": RUN_ID,
                        "state": "succeeded",
                        "run_revision": 3,
                        "attempt_count": 1,
                        "next_attempt_at": None,
                        "failure_reason_code": None,
                        "result_sha256": result_sha,
                        "observed_at": _timestamp(NOW + timedelta(seconds=1)),
                    }
                ],
            )
        assert request.url.path.endswith("/fail_scheduler_job_run")
        assert set(payload) == {
            "p_account_id",
            "p_holder_id",
            "p_outer_fencing_token",
            "p_release_sha",
            "p_run_id",
            "p_expected_run_revision",
            "p_definition_sha256",
            "p_lease_token",
            "p_failure_reason_code",
            "p_failure_sha256",
            "p_retryable",
        }
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": RUN_ID,
                    "state": "retry_wait",
                    "run_revision": 3,
                    "attempt_count": 1,
                    "next_attempt_at": _timestamp(NOW + timedelta(seconds=3)),
                    "failure_reason_code": "command_poll_retryable",
                    "result_sha256": FAILURE_SHA,
                    "observed_at": _timestamp(NOW + timedelta(seconds=1)),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    claim = _claim_from_response(_claim_response())
    assert claim.definition.definition_sha256 == definition.definition_sha256
    try:
        completed = await adapter.complete_job_run(
            claim,
            outer_lease=_outer_lease(),
            result_sha256=result_sha,
        )
        failed = await adapter.fail_job_run(
            claim,
            outer_lease=_outer_lease(),
            failure_reason_code="command_poll_retryable",
            failure_sha256=FAILURE_SHA,
            retryable=True,
        )
    finally:
        await client.aclose()

    assert completed.state == "succeeded"
    assert completed.run_revision == 3
    assert failed.state == "retry_wait"
    assert failed.next_attempt_at == NOW + timedelta(seconds=3)
    assert len(paths) == 2


async def test_adapter_accepts_idempotent_settlement_after_inner_lease_expiry() -> None:
    result_sha = "c" * 64
    recovered_at = NOW + timedelta(seconds=25)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complete_scheduler_job_run"):
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": RUN_ID,
                        "state": "succeeded",
                        "run_revision": 3,
                        "attempt_count": 1,
                        "next_attempt_at": None,
                        "failure_reason_code": None,
                        "result_sha256": result_sha,
                        "observed_at": _timestamp(recovered_at),
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": RUN_ID,
                    "state": "retry_wait",
                    "run_revision": 3,
                    "attempt_count": 1,
                    "next_attempt_at": _timestamp(NOW + timedelta(seconds=3)),
                    "failure_reason_code": "command_poll_retryable",
                    "result_sha256": FAILURE_SHA,
                    "observed_at": _timestamp(recovered_at),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    claim = _claim_from_response(_claim_response())
    renewed_outer = _outer_lease(expires_at=NOW + timedelta(seconds=60))
    try:
        completed = await adapter.complete_job_run(
            claim,
            outer_lease=renewed_outer,
            result_sha256=result_sha,
        )
        failed = await adapter.fail_job_run(
            claim,
            outer_lease=renewed_outer,
            failure_reason_code="command_poll_retryable",
            failure_sha256=FAILURE_SHA,
            retryable=True,
        )
    finally:
        await client.aclose()

    assert completed.observed_at == recovered_at
    assert failed.next_attempt_at == NOW + timedelta(seconds=3)
    assert failed.next_attempt_at < failed.observed_at


async def test_adapter_rejects_retry_schedule_before_claim_transition() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": RUN_ID,
                    "state": "retry_wait",
                    "run_revision": 3,
                    "attempt_count": 1,
                    "next_attempt_at": _timestamp(NOW + timedelta(seconds=1)),
                    "failure_reason_code": "command_poll_retryable",
                    "result_sha256": FAILURE_SHA,
                    "observed_at": _timestamp(NOW + timedelta(seconds=25)),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.fail_job_run(
                _claim_from_response(_claim_response()),
                outer_lease=_outer_lease(expires_at=NOW + timedelta(seconds=60)),
                failure_reason_code="command_poll_retryable",
                failure_sha256=FAILURE_SHA,
                retryable=True,
            )
    finally:
        await client.aclose()


async def test_adapter_rejects_retry_classification_outside_exact_job_matrix() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerInvariantError, match="classification_is_not_allowed"):
            await adapter.fail_job_run(
                _claim_from_response(_claim_response()),
                outer_lease=_outer_lease(),
                failure_reason_code="outbox_poll_retryable",
                failure_sha256=FAILURE_SHA,
                retryable=True,
            )
    finally:
        await client.aclose()

    assert calls == 0


async def test_adapter_treats_impossible_retry_receipt_as_unknown_outcome() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": RUN_ID,
                    "state": "dead_letter",
                    "run_revision": 3,
                    "attempt_count": 1,
                    "next_attempt_at": None,
                    "failure_reason_code": "command_poll_retryable",
                    "result_sha256": FAILURE_SHA,
                    "observed_at": _timestamp(NOW + timedelta(seconds=1)),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.fail_job_run(
                _claim_from_response(_claim_response()),
                outer_lease=_outer_lease(),
                failure_reason_code="command_poll_retryable",
                failure_sha256=FAILURE_SHA,
                retryable=True,
            )
    finally:
        await client.aclose()


async def test_adapter_rejects_non_exact_database_retry_delay() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": RUN_ID,
                    "state": "retry_wait",
                    "run_revision": 3,
                    "attempt_count": 1,
                    "next_attempt_at": _timestamp(NOW + timedelta(seconds=4)),
                    "failure_reason_code": "command_poll_retryable",
                    "result_sha256": FAILURE_SHA,
                    "observed_at": _timestamp(NOW + timedelta(seconds=1)),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.fail_job_run(
                _claim_from_response(_claim_response()),
                outer_lease=_outer_lease(),
                failure_reason_code="command_poll_retryable",
                failure_sha256=FAILURE_SHA,
                retryable=True,
            )
    finally:
        await client.aclose()


async def test_adapter_inspect_and_replay_bind_exact_dead_letter_evidence() -> None:
    definition = _definition()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = _request_payload(request)
        if request.url.path.endswith("/inspect_scheduler_dead_letter"):
            assert payload["p_source_run_id"] == RUN_ID
            return httpx.Response(
                200,
                json=[
                    {
                        "found": True,
                        "dead_letter": {
                            "source_run_id": RUN_ID,
                            "account_id": "paper-primary",
                            "job_key": "operations.commands",
                            "definition_sha256": definition.definition_sha256,
                            "source_revision": 3,
                            "attempt_count": 4,
                            "failure_reason_code": "scheduler_handler_unknown_failure",
                            "failure_sha256": FAILURE_SHA,
                            "replay_generation": 0,
                            "max_manual_replays": 1,
                            "dead_lettered_at": _timestamp(NOW - timedelta(seconds=1)),
                            "state": "dead_letter",
                        },
                        "eligible": True,
                        "ineligibility_reason": None,
                        "observed_at": _timestamp(NOW),
                    }
                ],
            )
        assert request.url.path.endswith("/replay_scheduler_dead_letter")
        assert set(payload) == {
            "p_account_id",
            "p_holder_id",
            "p_outer_fencing_token",
            "p_release_sha",
            "p_source_run_id",
            "p_expected_source_revision",
            "p_expected_definition_sha256",
            "p_expected_failure_reason_code",
            "p_expected_failure_sha256",
            "p_expected_replay_generation",
            "p_replay_request_id",
            "p_confirmed_reason_code",
            "p_explicit_confirmation",
        }
        assert payload["p_expected_source_revision"] == 3
        assert payload["p_expected_failure_reason_code"] == ("scheduler_handler_unknown_failure")
        assert payload["p_explicit_confirmation"] is True
        return httpx.Response(
            200,
            json=[
                {
                    "source_run_id": RUN_ID,
                    "new_run_id": NEW_RUN_ID,
                    "replay_request_id": REPLAY_REQUEST_ID,
                    "job_key": "operations.commands",
                    "definition_sha256": definition.definition_sha256,
                    "source_revision": 3,
                    "failure_reason_code": "scheduler_handler_unknown_failure",
                    "replay_generation": 1,
                    "state": "pending",
                    "created_at": _timestamp(NOW + timedelta(seconds=1)),
                    "observed_at": _timestamp(NOW + timedelta(seconds=1)),
                    "idempotent": False,
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        inspection = await adapter.inspect_dead_letter(
            RUN_ID,
            outer_lease=_outer_lease(),
        )
        assessment = inspection.assessment
        assert isinstance(assessment, SchedulerReplayAssessmentV1)
        receipt = await adapter.replay_dead_letter(
            assessment,
            replay_request_id=REPLAY_REQUEST_ID,
            confirmed_reason_code="scheduler_handler_unknown_failure",
            explicit_confirmation=True,
            outer_lease=_outer_lease(),
        )
    finally:
        await client.aclose()

    assert receipt.source_run_id == RUN_ID
    assert receipt.new_run_id == NEW_RUN_ID
    assert calls == 2


@pytest.mark.parametrize(
    ("account_id", "returned_source_run_id"),
    [
        ("paper-secondary", RUN_ID),
        ("paper-primary", NEW_RUN_ID),
    ],
    ids=["account-mismatch", "source-run-mismatch"],
)
async def test_adapter_rejects_inspection_binding_mismatch(
    account_id: str,
    returned_source_run_id: str,
) -> None:
    definition = _definition()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "found": True,
                    "dead_letter": {
                        "source_run_id": returned_source_run_id,
                        "account_id": account_id,
                        "job_key": "operations.commands",
                        "definition_sha256": definition.definition_sha256,
                        "source_revision": 3,
                        "attempt_count": 4,
                        "failure_reason_code": "scheduler_handler_unknown_failure",
                        "failure_sha256": FAILURE_SHA,
                        "replay_generation": 0,
                        "max_manual_replays": 1,
                        "dead_lettered_at": _timestamp(NOW - timedelta(seconds=1)),
                        "state": "dead_letter",
                    },
                    "eligible": True,
                    "ineligibility_reason": None,
                    "observed_at": _timestamp(NOW),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.inspect_dead_letter(RUN_ID, outer_lease=_outer_lease())
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("job_key", "max_attempts", "max_manual_replays"),
    [
        ("operations.execution", 1, 1),
        ("operations.commands", 3, 2),
    ],
)
async def test_adapter_treats_unsafe_replay_budget_as_invalid_success(
    job_key: str,
    max_attempts: int,
    max_manual_replays: int,
) -> None:
    definition = ScheduledJobDefinitionV1(
        job_key=cast(Any, job_key),
        interval_seconds=2,
        lease_ttl_seconds=30,
        max_attempts=max_attempts,
        retry_base_seconds=2,
        retry_max_seconds=8,
        max_manual_replays=max_manual_replays,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "found": True,
                    "dead_letter": {
                        "source_run_id": RUN_ID,
                        "account_id": "paper-primary",
                        "job_key": job_key,
                        "definition_sha256": definition.definition_sha256,
                        "source_revision": 3,
                        "attempt_count": 1,
                        "failure_reason_code": "scheduler_lease_expired",
                        "failure_sha256": FAILURE_SHA,
                        "replay_generation": 0,
                        "max_manual_replays": max_manual_replays,
                        "dead_lettered_at": _timestamp(NOW - timedelta(seconds=1)),
                        "state": "dead_letter",
                    },
                    "eligible": True,
                    "ineligibility_reason": None,
                    "observed_at": _timestamp(NOW),
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.inspect_dead_letter(RUN_ID, outer_lease=_outer_lease())
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'[{"claimed":false,"claim":null,"observed_at":"2026-07-24T03:00:00Z"},'
        b'{"claimed":false,"claim":null,"observed_at":"2026-07-24T03:00:00Z"}]',
        b'[{"claimed":false,"claimed":true,"claim":null,"observed_at":"2026-07-24T03:00:00Z"}]',
        b'[{"claimed":false,"claim":null,"observed_at":"2026-07-24T03:00:00Z",'
        b'"extra":"forbidden"}]',
    ],
    ids=["zero-row", "multiple-row", "duplicate-key", "extra-key"],
)
async def test_adapter_rejects_row_count_duplicate_and_non_exact_shapes(body: bytes) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()


async def test_adapter_normalizes_overflowing_timestamp_as_unknown_outcome() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "claimed": False,
                    "claim": None,
                    "observed_at": "0001-01-01T00:00:00+23:59",
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError) as captured:
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()

    assert captured.value.__cause__ is None


async def test_adapter_owned_client_disables_environment_proxy_discovery() -> None:
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
    )
    try:
        assert adapter.transport_is_managed is True
        assert getattr(cast(Any, adapter)._client, "_trust_env", None) is False
    finally:
        await adapter.close()


async def test_adapter_rejects_oversized_or_compressed_response() -> None:
    responses = iter(
        (
            httpx.Response(
                200,
                content=b" " * (DURABLE_SCHEDULER_MAX_RPC_RESPONSE_BYTES + 1),
            ),
            httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                content=gzip.compress(b"not-trusted"),
            ),
        )
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        for _ in range(2):
            with pytest.raises(
                SchedulerMutationOutcomeUnknownError,
                match="scheduler_mutation_outcome_unknown",
            ):
                await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()


async def test_adapter_streams_size_bound_before_buffer_and_always_closes() -> None:
    stream = _TrackedAsyncStream(
        [
            b"x" * (DURABLE_SCHEDULER_MAX_RPC_RESPONSE_BYTES // 2),
            b"y" * (DURABLE_SCHEDULER_MAX_RPC_RESPONSE_BYTES // 2),
            b"z",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()

    assert stream.closed is True
    assert stream.yielded_chunks == 3


async def test_adapter_disables_redirects_per_mutating_request() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            307,
            headers={"location": "http://127.0.0.1:54321/redirected"},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()

    assert calls == ["/rest/v1/rpc/claim_due_scheduler_job"]


def test_adapter_has_fixed_allowlist_and_requires_service_credentials() -> None:
    assert {
        "ensure_scheduler_job_definition",
        "converge_scheduler_job_definition",
        "claim_due_scheduler_job",
        "complete_scheduler_job_run",
        "fail_scheduler_job_run",
        "inspect_scheduler_dead_letter",
        "replay_scheduler_dead_letter",
    } == DURABLE_SCHEDULER_RPC_ALLOWLIST
    with pytest.raises(SchedulerInvariantError, match="credentials_are_missing"):
        SupabaseDurableScheduler(Settings(), release_sha=RELEASE_SHA)
    with pytest.raises(SchedulerInvariantError, match="release_sha"):
        SupabaseDurableScheduler(_settings(), release_sha="not-a-release")


@pytest.mark.parametrize(
    ("url", "environment"),
    [
        ("https://attacker.example", "production"),
        ("https://project.supabase.co.evil.example", "production"),
        ("http://project.supabase.co", "local"),
        ("http://192.168.0.10:54321", "local"),
        ("http://localhost:54321", "production"),
        ("https://user:password@project.supabase.co", "production"),
        ("https://project.supabase.co/rest/v1", "production"),
        ("https://project.supabase.co?token=secret", "production"),
        ("https://project.supabase.co:444", "production"),
    ],
)
def test_adapter_rejects_non_allowlisted_service_role_origins(
    url: str,
    environment: str,
) -> None:
    with pytest.raises(SchedulerInvariantError, match="origin_is_not_allowed"):
        SupabaseDurableScheduler(
            _settings(url=url, environment=environment),
            release_sha=RELEASE_SHA,
        )


@pytest.mark.parametrize(
    ("url", "environment"),
    [
        ("https://project.supabase.co", "production"),
        ("https://project.supabase.co:443", "production"),
        ("http://127.0.0.1:54321", "local"),
        ("http://localhost:54321", "test"),
        ("http://[::1]:54321", "local"),
    ],
)
async def test_adapter_accepts_only_hosted_or_local_loopback_origins(
    url: str,
    environment: str,
) -> None:
    adapter = SupabaseDurableScheduler(
        _settings(url=url, environment=environment),
        release_sha=RELEASE_SHA,
    )
    try:
        assert adapter.persistence_authority.startswith("supabase-worker-api:")
    finally:
        await adapter.close()


async def test_adapter_exposes_read_only_persistence_authority() -> None:
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
    )
    try:
        authority = adapter.persistence_authority
        assert authority.startswith("supabase-worker-api:")
        mutable_adapter = cast(Any, adapter)
        with pytest.raises(AttributeError):
            mutable_adapter.persistence_authority = "supabase-worker-api:" + "f" * 64
        assert adapter.persistence_authority == authority
    finally:
        await adapter.close()


async def test_adapter_transport_origin_and_client_are_read_only() -> None:
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
    )
    replacement = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
    )
    try:
        original_url = adapter.base_url
        mutable_adapter = cast(Any, adapter)

        with pytest.raises(AttributeError):
            mutable_adapter.base_url = "https://attacker.invalid/rest/v1/rpc"
        with pytest.raises(AttributeError):
            mutable_adapter.transport_is_managed = False
        with pytest.raises(AttributeError):
            mutable_adapter._client = replacement
        with pytest.raises(AttributeError):
            mutable_adapter._managed_client = replacement

        assert adapter.base_url == original_url
        assert adapter.transport_is_managed is True
        assert not hasattr(adapter, "client")
        assert not hasattr(adapter, "codec")
        assert not hasattr(adapter, "__dict__")
        with pytest.raises(AttributeError):
            delattr(adapter, "_client")
    finally:
        await replacement.aclose()
        await adapter.close()


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (409, SchedulerTransitionRejectedError),
        (302, SchedulerMutationOutcomeUnknownError),
        (408, SchedulerMutationOutcomeUnknownError),
        (425, SchedulerMutationOutcomeUnknownError),
        (429, SchedulerMutationOutcomeUnknownError),
        (503, SchedulerMutationOutcomeUnknownError),
    ],
)
async def test_adapter_separates_deterministic_rejection_from_unknown_outcome(
    status: int,
    expected_error: type[Exception],
) -> None:
    secret = "database-secret-must-not-leak"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=secret.encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(expected_error) as captured:
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()

    assert secret not in "".join(traceback.format_exception(captured.value))
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("status", [201, 202, 204, 206])
async def test_adapter_requires_exact_200_even_for_exact_shaped_success_body(
    status: int,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=_claim_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()


def test_adapter_status_classifier_is_explicit_and_excludes_uncertain_statuses() -> None:
    assert 409 in DURABLE_SCHEDULER_DETERMINISTIC_REJECTION_STATUSES
    assert {302, 408, 425, 429, 500}.isdisjoint(
        DURABLE_SCHEDULER_DETERMINISTIC_REJECTION_STATUSES
    )


async def test_adapter_transport_failure_is_unknown_and_cancellation_propagates() -> None:
    failures: list[BaseException] = [
        RuntimeError("transport-secret"),
        asyncio.CancelledError(),
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise failures.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError) as captured:
            await adapter.claim_due_job(outer_lease=_outer_lease())
        with pytest.raises(asyncio.CancelledError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()

    assert "transport-secret" not in "".join(traceback.format_exception(captured.value))


async def test_adapter_enforces_total_timeout_independent_of_injected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS <= 5

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=_claim_response())

    monkeypatch.setattr(
        "app.adapters.persistence.supabase_durable_scheduler."
        "DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS",
        0.01,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=None,
    )
    adapter = SupabaseDurableScheduler(
        _settings(),
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(SchedulerMutationOutcomeUnknownError):
            await adapter.claim_due_job(outer_lease=_outer_lease())
    finally:
        await client.aclose()


class _TrackedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded_chunks = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded_chunks += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _settings(
    *,
    url: str = "http://127.0.0.1:54321",
    environment: str = "local",
) -> Settings:
    settings = Settings.model_validate(
        {
            "ENV": "local",
            "SUPABASE_URL": url,
            "SUPABASE_SECRET_KEY": SecretStr("service-role-test-secret"),
        }
    )
    return settings.model_copy(update={"env": environment})


def _definition() -> ScheduledJobDefinitionV1:
    return ScheduledJobDefinitionV1(
        job_key="operations.commands",
        interval_seconds=2,
        lease_ttl_seconds=30,
        max_attempts=3,
        retry_base_seconds=2,
        retry_max_seconds=8,
        max_manual_replays=1,
    )


def _outer_lease(
    *,
    acquired_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> WorkerLease:
    return WorkerLease(
        account_id="paper-primary",
        holder_id=HOLDER_ID,
        fencing_token=7,
        acquired_at=acquired_at or NOW - timedelta(seconds=10),
        expires_at=expires_at or NOW + timedelta(seconds=20),
    )


def _claim_response() -> list[object]:
    definition = _definition()
    return [
        {
            "claimed": True,
            "claim": {
                "definition": {
                    **definition.to_payload(),
                    "definition_sha256": definition.definition_sha256,
                },
                "run": {
                    "run_id": RUN_ID,
                    "account_id": "paper-primary",
                    "job_key": definition.job_key,
                    "definition_sha256": definition.definition_sha256,
                    "state": "leased",
                    "revision": 2,
                    "attempt_count": 1,
                    "replay_generation": 0,
                    "replay_of_run_id": None,
                    "scheduled_for": _timestamp(NOW - timedelta(seconds=2)),
                    "available_at": _timestamp(NOW - timedelta(seconds=2)),
                    "created_at": _timestamp(NOW - timedelta(seconds=2)),
                    "updated_at": _timestamp(NOW),
                },
                "lease": {
                    "lease_token": LEASE_TOKEN,
                    "run_id": RUN_ID,
                    "account_id": "paper-primary",
                    "holder_id": HOLDER_ID,
                    "release_sha": RELEASE_SHA,
                    "outer_fencing_token": 7,
                    "attempt_number": 1,
                    "run_revision": 2,
                    "leased_at": _timestamp(NOW),
                    "lease_expires_at": _timestamp(NOW + timedelta(seconds=20)),
                },
            },
            "observed_at": _timestamp(NOW),
        }
    ]


def _convergence_response(definition: ScheduledJobDefinitionV1) -> list[object]:
    return [
        {
            "status": "converged",
            "definition": {
                **definition.to_payload(),
                "definition_sha256": definition.definition_sha256,
                "definition_id": DEFINITION_ID,
                "account_id": "paper-primary",
                "revision": 1,
                "next_due_at": _timestamp(NOW),
                "scheduler_state": "ready",
            },
            "claim": None,
            "active_run_id": None,
            "next_eligible_at": None,
            "reason_code": None,
            "observed_at": _timestamp(NOW),
        }
    ]


def _claim_from_response(response: list[object]) -> ScheduledJobClaimV1:
    raw = cast(dict[str, Any], response[0])
    claim = cast(dict[str, Any], raw["claim"])
    definition_row = cast(dict[str, Any], claim["definition"])
    run_row = cast(dict[str, Any], claim["run"])
    lease_row = cast(dict[str, Any], claim["lease"])
    definition = ScheduledJobDefinitionV1(
        job_key=definition_row["job_key"],
        interval_seconds=definition_row["interval_seconds"],
        lease_ttl_seconds=definition_row["lease_ttl_seconds"],
        max_attempts=definition_row["max_attempts"],
        retry_base_seconds=definition_row["retry_base_seconds"],
        retry_max_seconds=definition_row["retry_max_seconds"],
        max_manual_replays=definition_row["max_manual_replays"],
        enabled=definition_row["enabled"],
    )
    return ScheduledJobClaimV1(
        definition=definition,
        run=ScheduledJobRunV1(
            run_id=run_row["run_id"],
            account_id=run_row["account_id"],
            job_key=run_row["job_key"],
            definition_sha256=run_row["definition_sha256"],
            state=run_row["state"],
            revision=run_row["revision"],
            attempt_count=run_row["attempt_count"],
            replay_generation=run_row["replay_generation"],
            replay_of_run_id=run_row["replay_of_run_id"],
            scheduled_for=datetime.fromisoformat(run_row["scheduled_for"].replace("Z", "+00:00")),
            available_at=datetime.fromisoformat(run_row["available_at"].replace("Z", "+00:00")),
            created_at=datetime.fromisoformat(run_row["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(run_row["updated_at"].replace("Z", "+00:00")),
        ),
        lease=ScheduledJobLeaseV1(
            lease_token=lease_row["lease_token"],
            run_id=lease_row["run_id"],
            account_id=lease_row["account_id"],
            holder_id=lease_row["holder_id"],
            release_sha=lease_row["release_sha"],
            outer_fencing_token=lease_row["outer_fencing_token"],
            attempt_number=lease_row["attempt_number"],
            run_revision=lease_row["run_revision"],
            leased_at=datetime.fromisoformat(lease_row["leased_at"].replace("Z", "+00:00")),
            lease_expires_at=datetime.fromisoformat(
                lease_row["lease_expires_at"].replace("Z", "+00:00")
            ),
        ),
        observed_at=NOW,
    )


def _request_payload(request: httpx.Request) -> dict[str, object]:
    value = json.loads(request.content)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

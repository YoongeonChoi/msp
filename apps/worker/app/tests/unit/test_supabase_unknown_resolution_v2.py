from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_worker_api import SupabaseWorkerApi
from app.config import Settings
from app.domain.execution_v2.models import ExecutionInvariantError
from app.domain.execution_v2.unknown_resolution import (
    UnknownResolutionApplyAmbiguousError,
    UnknownResolutionCandidate,
    UnknownResolutionClaim,
)

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
HOLDER_ID = "00000000-0000-4000-8000-000000000001"
RELEASE_SHA = "a" * 40


async def test_worker_api_lists_unknown_resolution_with_exact_dedicated_contract() -> None:
    command_id = str(uuid4())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_list_row(command_id)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        candidates = await adapter.list_unknown_resolution_candidates(
            account_id="paper-primary",
            environment="paper",
            holder_id=HOLDER_ID,
            release_sha=RELEASE_SHA,
            fencing_token=7,
            now=NOW,
            limit=3,
        )

    assert len(candidates) == 1
    assert candidates[0].account_id == "paper-primary"
    assert candidates[0].environment == "paper"
    assert candidates[0].lease_fencing_token == 7
    assert seen[0].url.path == "/rest/v1/rpc/list_unknown_resolution_v2"
    assert json.loads(seen[0].content) == {
        "account_id": "paper-primary",
        "holder_id": HOLDER_ID,
        "release_sha": RELEASE_SHA,
        "fencing_token": 7,
        "result_limit": 3,
        "observed_at": NOW.isoformat(),
    }


async def test_worker_api_rejects_unknown_list_with_unknown_field() -> None:
    row = _list_row(str(uuid4())) | {"silent_default": 0}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[row]))
    ) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="response_fields_are_invalid"):
            await adapter.list_unknown_resolution_candidates(
                account_id="paper-primary",
                environment="paper",
                holder_id=HOLDER_ID,
                release_sha=RELEASE_SHA,
                fencing_token=7,
                now=NOW,
                limit=3,
            )


async def test_worker_api_claim_binds_revisions_and_claim_token() -> None:
    candidate = _candidate()
    claim_token = str(uuid4())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "command_id": candidate.command_id,
                    "claim_token": claim_token,
                    "command_revision": candidate.command_revision + 1,
                    "work_revision": candidate.work_revision + 1,
                    "claim_expires_at": (NOW + timedelta(seconds=30)).isoformat(),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        claim = await adapter.claim_unknown_resolution(candidate, now=NOW)

    assert claim.claim_token == claim_token
    assert seen[0].url.path == "/rest/v1/rpc/claim_unknown_resolution_v2"
    assert json.loads(seen[0].content) == {
        "command_id": candidate.command_id,
        "holder_id": HOLDER_ID,
        "release_sha": RELEASE_SHA,
        "fencing_token": 7,
        "expected_command_revision": 2,
        "expected_work_revision": 0,
        "claimed_at": NOW.isoformat(),
    }


async def test_worker_api_rejects_claim_revision_that_did_not_advance_exactly() -> None:
    candidate = _candidate()
    row = {
        "command_id": candidate.command_id,
        "claim_token": str(uuid4()),
        "command_revision": candidate.command_revision + 2,
        "work_revision": candidate.work_revision + 1,
        "claim_expires_at": (NOW + timedelta(seconds=30)).isoformat(),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[row]))
    ) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="postcondition_mismatch"):
            await adapter.claim_unknown_resolution(candidate, now=NOW)


async def test_worker_api_apply_binds_claim_fence_epoch_and_exact_receipt() -> None:
    claim = _claim()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_apply_row(claim, inserted=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        receipt = await adapter.apply_unknown_resolution(
            claim,
            now=NOW,
            replay=False,
        )

    assert receipt.inserted is True
    assert receipt.replayed is False
    assert json.loads(seen[0].content) == {
        "command_id": claim.command_id,
        "claim_token": claim.claim_token,
        "holder_id": HOLDER_ID,
        "release_sha": RELEASE_SHA,
        "fencing_token": 7,
        "expected_command_revision": claim.command_revision,
        "expected_work_revision": claim.work_revision,
        "expected_control_epoch": claim.expected_control_epoch,
        "applied_at": NOW.isoformat(),
    }


async def test_worker_api_apply_ambiguous_then_checks_post_state_noop_revision() -> None:
    claim = _claim()
    calls = 0
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payloads.append(json.loads(request.content))
        if calls == 1:
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(200, json=_apply_row(claim, inserted=False))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        with pytest.raises(UnknownResolutionApplyAmbiguousError):
            await adapter.apply_unknown_resolution(claim, now=NOW, replay=False)
        receipt = await adapter.apply_unknown_resolution(claim, now=NOW, replay=True)

    assert receipt.replayed is True
    assert payloads[1]["expected_command_revision"] == claim.command_revision + 1
    assert payloads[1]["expected_work_revision"] == claim.work_revision + 1


async def test_worker_api_treats_mismatched_apply_token_as_ambiguous() -> None:
    claim = _claim()
    row = _apply_row(claim, inserted=True) | {"claim_token": str(uuid4())}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=row))
    ) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        with pytest.raises(UnknownResolutionApplyAmbiguousError):
            await adapter.apply_unknown_resolution(claim, now=NOW, replay=False)


async def test_worker_api_keeps_stale_apply_rejection_non_ambiguous() -> None:
    claim = _claim()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                409,
                json={"message": "unknown_resolution_v2_apply_gate_stale"},
            )
        )
    ) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="rpc_rejected"):
            await adapter.apply_unknown_resolution(claim, now=NOW, replay=False)


async def test_worker_api_rejects_account_environment_mismatch_before_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="account_environment_mismatch"):
            await adapter.list_unknown_resolution_candidates(
                account_id="contract-test-primary",
                environment="paper",
                holder_id=HOLDER_ID,
                release_sha=RELEASE_SHA,
                fencing_token=7,
                now=NOW,
                limit=3,
            )
    assert called is False


async def test_worker_api_rejects_claim_bound_to_another_release_before_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SupabaseWorkerApi(
            _settings(),
            release_sha=RELEASE_SHA,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="release_sha_mismatch"):
            await adapter.apply_unknown_resolution(
                replace(_claim(), release_sha="e" * 40),
                now=NOW,
                replay=False,
            )
    assert called is False


def _settings() -> Settings:
    return Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID=HOLDER_ID,
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )


def _candidate() -> UnknownResolutionCandidate:
    command_id = str(uuid4())
    return UnknownResolutionCandidate(
        command_id=command_id,
        request_id=command_id,
        review_id=str(uuid4()),
        break_id=str(uuid4()),
        intent_id=str(uuid4()),
        terminal_status="filled",
        request_payload_sha256="b" * 64,
        review_payload_sha256="c" * 64,
        command_revision=2,
        work_revision=0,
        work_state="approved",
        claim_token=None,
        claim_expires_at=None,
        expected_control_epoch=4,
        account_id="paper-primary",
        environment="paper",
        holder_id=HOLDER_ID,
        release_sha=RELEASE_SHA,
        lease_fencing_token=7,
    )


def _claim() -> UnknownResolutionClaim:
    candidate = _candidate()
    return UnknownResolutionClaim(
        command_id=candidate.command_id,
        request_id=candidate.request_id,
        review_id=candidate.review_id,
        break_id=candidate.break_id,
        intent_id=candidate.intent_id,
        terminal_status=candidate.terminal_status,
        request_payload_sha256=candidate.request_payload_sha256,
        review_payload_sha256=candidate.review_payload_sha256,
        command_revision=3,
        work_revision=1,
        claim_token=str(uuid4()),
        claim_expires_at=NOW + timedelta(seconds=30),
        expected_control_epoch=candidate.expected_control_epoch,
        account_id=candidate.account_id,
        environment=candidate.environment,
        holder_id=candidate.holder_id,
        release_sha=candidate.release_sha,
        lease_fencing_token=candidate.lease_fencing_token,
    )


def _list_row(command_id: str) -> dict[str, object]:
    return {
        "command_id": command_id,
        "request_id": command_id,
        "review_id": str(uuid4()),
        "break_id": str(uuid4()),
        "intent_id": str(uuid4()),
        "terminal_status": "filled",
        "request_payload_sha256": "b" * 64,
        "review_payload_sha256": "c" * 64,
        "command_revision": 2,
        "work_revision": 0,
        "work_state": "approved",
        "claim_token": None,
        "claim_expires_at": None,
        "expected_control_epoch": 4,
    }


def _apply_row(claim: UnknownResolutionClaim, *, inserted: bool) -> dict[str, object]:
    return {
        "schema_version": 2,
        "command_id": claim.command_id,
        "break_id": claim.break_id,
        "intent_id": claim.intent_id,
        "state": "applied",
        "receipt_revision": claim.command_revision + 1,
        "break_revision": 5,
        "request_digest_sha256": claim.request_payload_sha256,
        "review_digest_sha256": claim.review_payload_sha256,
        "terminal_status": claim.terminal_status,
        "claim_token": claim.claim_token,
        "work_revision": claim.work_revision + 1,
        "application_id": str(uuid4()),
        "application_sha256": "d" * 64,
        "accounting_mutation_allowed": True,
        "resolution_complete": True,
        "inserted": inserted,
    }

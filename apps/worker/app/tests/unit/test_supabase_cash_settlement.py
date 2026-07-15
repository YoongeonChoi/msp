from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_worker_api import SupabaseWorkerApi
from app.config import Settings
from app.domain.execution_v2.cash_settlement import (
    CashSettlementCompletionAmbiguousError,
    CashSettlementCompletionRetryableError,
)
from app.domain.execution_v2.models import ExecutionInvariantError

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
WORKER_ID = "00000000-0000-4000-8000-000000000001"
OBLIGATION_ID = "00000000-0000-4000-8000-000000000010"
FILL_ID = "00000000-0000-4000-8000-000000000011"
INTENT_ID = "00000000-0000-4000-8000-000000000012"
CLAIM_TOKEN = "00000000-0000-4000-8000-000000000013"


async def test_worker_api_runs_cash_settlement_claim_complete_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/claim_cash_settlement_batch"):
            return httpx.Response(200, json=[_claim_row()])
        if request.url.path.endswith("/complete_cash_settlement"):
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "obligation_id": OBLIGATION_ID,
                    "settlement_transaction_id": (
                        "00000000-0000-4000-8000-000000000099"
                    ),
                    "claim_revision": 1,
                    "settled_revision": 2,
                    "obligation_type": "cash_payable",
                    "amount_krw": 10_010,
                    "settlement_date": "2026-07-15",
                    "settled_at": NOW.isoformat(),
                    "replayed": False,
                },
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = SupabaseWorkerApi(_settings(), release_sha="a" * 40, client=client)
        claims = await api.claim_cash_settlement_batch(
            account_id="paper-primary",
            holder_id=WORKER_ID,
            release_sha="a" * 40,
            fencing_token=7,
            now=NOW,
            limit=20,
        )
        receipt = await api.complete_cash_settlement(
            claims[0],
            holder_id=WORKER_ID,
            release_sha="a" * 40,
            fencing_token=7,
            now=NOW,
        )

    assert claims[0].settlement_date == date(2026, 7, 15)
    assert receipt.amount_krw == 10_010
    assert json.loads(seen[0].content)["p_fencing_token"] == 7
    assert json.loads(seen[1].content)["p_expected_revision"] == 1


async def test_worker_api_parses_cash_settlement_retry_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim_cash_settlement_batch"):
            return httpx.Response(200, json=[_claim_row()])
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "obligation_id": OBLIGATION_ID,
                "claim_revision": 1,
                "revision": 2,
                "state": "pending",
                "attempt_count": 1,
                "available_at": (NOW + timedelta(seconds=5)).isoformat(),
                "error_code": "settlement_worker_error",
                "replayed": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = SupabaseWorkerApi(_settings(), release_sha="a" * 40, client=client)
        claim = (
            await api.claim_cash_settlement_batch(
                account_id="paper-primary",
                holder_id=WORKER_ID,
                release_sha="a" * 40,
                fencing_token=7,
                now=NOW,
                limit=20,
            )
        )[0]
        receipt = await api.fail_cash_settlement_attempt(
            claim,
            holder_id=WORKER_ID,
            release_sha="a" * 40,
            fencing_token=7,
            now=NOW,
            error_code="settlement_worker_error",
        )

    assert receipt.state == "pending"
    assert receipt.revision == 2


@pytest.mark.parametrize("failure", ["timeout", "malformed"])
async def test_complete_transport_or_response_ambiguity_never_becomes_failure_receipt(
    failure: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim_cash_settlement_batch"):
            return httpx.Response(200, json=[_claim_row()])
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, content=b"not-json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = SupabaseWorkerApi(_settings(), release_sha="a" * 40, client=client)
        claim = (
            await api.claim_cash_settlement_batch(
                account_id="paper-primary",
                holder_id=WORKER_ID,
                release_sha="a" * 40,
                fencing_token=7,
                now=NOW,
                limit=1,
            )
        )[0]
        with pytest.raises(CashSettlementCompletionAmbiguousError):
            await api.complete_cash_settlement(
                claim,
                holder_id=WORKER_ID,
                release_sha="a" * 40,
                fencing_token=7,
                now=NOW,
            )


async def test_complete_maps_only_proven_projection_rollback_to_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim_cash_settlement_batch"):
            return httpx.Response(200, json=[_claim_row()])
        return httpx.Response(
            409,
            json={
                "code": "23514",
                "details": None,
                "hint": None,
                "message": "cash_settlement_projection_maturity_failed",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = SupabaseWorkerApi(_settings(), release_sha="a" * 40, client=client)
        claim = (
            await api.claim_cash_settlement_batch(
                account_id="paper-primary",
                holder_id=WORKER_ID,
                release_sha="a" * 40,
                fencing_token=7,
                now=NOW,
                limit=1,
            )
        )[0]
        with pytest.raises(CashSettlementCompletionRetryableError) as raised:
            await api.complete_cash_settlement(
                claim,
                holder_id=WORKER_ID,
                release_sha="a" * 40,
                fencing_token=7,
                now=NOW,
            )

    assert raised.value.failure_code == "settlement_projection_conflict"


async def test_complete_does_not_classify_other_database_rejection_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim_cash_settlement_batch"):
            return httpx.Response(200, json=[_claim_row()])
        return httpx.Response(
            409,
            json={
                "code": "40001",
                "message": "cash_settlement_claim_not_owned_current_or_expired",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = SupabaseWorkerApi(_settings(), release_sha="a" * 40, client=client)
        claim = (
            await api.claim_cash_settlement_batch(
                account_id="paper-primary",
                holder_id=WORKER_ID,
                release_sha="a" * 40,
                fencing_token=7,
                now=NOW,
                limit=1,
            )
        )[0]
        with pytest.raises(
            ExecutionInvariantError,
            match="cash_settlement_rpc_rejected",
        ):
            await api.complete_cash_settlement(
                claim,
                holder_id=WORKER_ID,
                release_sha="a" * 40,
                fencing_token=7,
                now=NOW,
            )


def _settings() -> Settings:
    return Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID=WORKER_ID,
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )


def _claim_row() -> dict[str, object]:
    return {
        "obligation_id": OBLIGATION_ID,
        "fill_id": FILL_ID,
        "intent_id": INTENT_ID,
        "account_id": "paper-primary",
        "environment": "paper",
        "obligation_type": "cash_payable",
        "amount_krw": 10_010,
        "settlement_date": "2026-07-15",
        "obligation_sha256": "b" * 64,
        "revision": 1,
        "claim_token": CLAIM_TOKEN,
        "claim_expires_at": (NOW + timedelta(seconds=30)).isoformat(),
    }

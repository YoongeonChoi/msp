from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_paper_execution_producer import (
    SupabasePaperExecutionProducer,
)
from app.config import Settings
from app.domain.execution_v2.models import ExecutionInvariantError
from app.tests.unit.test_publish_paper_execution_source_v1 import (
    COMMAND_ID,
    FIXTURE_ID,
    INTENT_ID,
    SERIES_ID,
    SHA_A,
    SHA_B,
    WORKER_ID,
    _candidate,
    _fixture,
)

RELEASE_SHA = "9" * 40


@pytest.mark.asyncio
async def test_adapter_uses_only_worker_rpc_and_parses_strict_receipts() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["content-profile"] == "worker_api"
        if request.url.path.endswith("/ingest_paper_bar_fixture_v1"):
            assert payload["p_fixture"]["series_id"] == SERIES_ID
            return httpx.Response(
                200,
                json=[
                    {
                        "series_id": SERIES_ID,
                        "fixture_set_id": FIXTURE_ID,
                        "batch_sequence": 1,
                        "bar_count": 1,
                        "fixture_sha256": SHA_A,
                        "idempotent": False,
                    }
                ],
            )
        assert request.url.path.endswith("/enqueue_paper_execution_candidate_v1")
        assert payload["p_fencing_token"] == 7
        assert payload["p_control_epoch"] == 3
        return httpx.Response(
            200,
            json=[
                {
                    "command_id": COMMAND_ID,
                    "intent_id": INTENT_ID,
                    "state": "pending",
                    "source_revision": 1,
                    "semantic_key_sha256": SHA_B,
                    "idempotent": False,
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    producer = SupabasePaperExecutionProducer(
        _settings(),
        account_id="paper-primary",
        release_sha=RELEASE_SHA,
        client=client,
    )
    now = datetime(2026, 7, 15, tzinfo=UTC)
    try:
        fixture = await producer.ingest_bar_fixture(
            _fixture(), worker_id=WORKER_ID, now=now
        )
        candidate = await producer.enqueue_candidate(
            _candidate(),
            worker_id=WORKER_ID,
            fencing_token=7,
            control_epoch=3,
            now=now,
        )
    finally:
        await client.aclose()

    assert fixture.fixture_set_id == FIXTURE_ID
    assert candidate.command_id == COMMAND_ID
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "ingest_paper_bar_fixture_v1",
        "enqueue_paper_execution_candidate_v1",
    ]


@pytest.mark.asyncio
async def test_adapter_rejects_extra_receipt_fields() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "series_id": SERIES_ID,
                    "fixture_set_id": FIXTURE_ID,
                    "batch_sequence": 1,
                    "bar_count": 1,
                    "fixture_sha256": SHA_A,
                    "idempotent": False,
                    "unexpected": True,
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    producer = SupabasePaperExecutionProducer(
        _settings(),
        account_id="paper-primary",
        release_sha=RELEASE_SHA,
        client=client,
    )
    try:
        with pytest.raises(ExecutionInvariantError, match="result_shape"):
            await producer.ingest_bar_fixture(
                _fixture(),
                worker_id=WORKER_ID,
                now=datetime(2026, 7, 15, tzinfo=UTC),
            )
    finally:
        await client.aclose()


def test_adapter_requires_explicit_source_input_flag() -> None:
    settings = _settings(EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED=False)

    with pytest.raises(ExecutionInvariantError, match="input_is_not_enabled"):
        SupabasePaperExecutionProducer(
            settings,
            account_id="paper-primary",
            release_sha=RELEASE_SHA,
        )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "EXECUTION_V2_ENABLED": True,
        "EXECUTION_V2_WORKER_API_ENABLED": True,
        "EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED": True,
        "EXECUTION_V2_WORKER_ID": WORKER_ID,
        "EXECUTION_V2_ACCOUNT_ID": "paper-primary",
        "SUPABASE_URL": "http://127.0.0.1:54321",
        "SUPABASE_SECRET_KEY": SecretStr("test-secret"),
    }
    values.update(overrides)
    return Settings.model_validate(values)

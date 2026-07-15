from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.ports.paper_execution_producer_port import (
    PaperBarFixtureReceipt,
    PaperCandidateReceipt,
)
from app.application.use_cases.publish_paper_execution_source_v1 import (
    PublishPaperExecutionSourceV1,
)
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import ExecutionInvariantError

WORKER_ID = "00000000-0000-4000-8000-000000000001"
SERIES_ID = "00000000-0000-4000-8000-000000000002"
FIXTURE_ID = "00000000-0000-4000-8000-000000000003"
INTENT_ID = "00000000-0000-4000-8000-000000000004"
COMMAND_ID = "00000000-0000-4000-8000-000000000005"
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.mark.asyncio
async def test_publication_ingests_fixture_before_fenced_candidate() -> None:
    producer = FakeProducer()
    publication = await PublishPaperExecutionSourceV1(
        producer,
        account_id="paper-primary",
        worker_id=WORKER_ID,
    ).publish(
        fixture=_fixture(),
        candidate=_candidate(),
        fencing_token=7,
        control_epoch=3,
        now=datetime(2026, 7, 15, tzinfo=UTC),
    )

    assert producer.calls == ["fixture", "candidate"]
    assert producer.fencing_token == 7
    assert producer.control_epoch == 3
    assert publication.fixture.fixture_set_id == FIXTURE_ID
    assert publication.candidate.command_id == COMMAND_ID


@pytest.mark.asyncio
async def test_publication_rejects_cross_artifact_mismatch_before_mutation() -> None:
    producer = FakeProducer()
    candidate = _candidate()
    candidate["symbol"] = "000660"

    with pytest.raises(ExecutionInvariantError, match="symbol_mismatch"):
        await PublishPaperExecutionSourceV1(
            producer,
            account_id="paper-primary",
            worker_id=WORKER_ID,
        ).publish(
            fixture=_fixture(),
            candidate=candidate,
            fencing_token=7,
            control_epoch=3,
            now=datetime(2026, 7, 15, tzinfo=UTC),
        )

    assert producer.calls == []


@pytest.mark.asyncio
async def test_publication_rejects_unknown_fields_before_mutation() -> None:
    producer = FakeProducer()
    fixture = _fixture()
    fixture["unexpected"] = True

    with pytest.raises(ExecutionInvariantError, match="fixture_schema"):
        await PublishPaperExecutionSourceV1(
            producer,
            account_id="paper-primary",
            worker_id=WORKER_ID,
        ).publish(
            fixture=fixture,
            candidate=_candidate(),
            fencing_token=7,
            control_epoch=3,
            now=datetime(2026, 7, 15, tzinfo=UTC),
        )

    assert producer.calls == []


class FakeProducer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fencing_token: int | None = None
        self.control_epoch: int | None = None

    async def ingest_bar_fixture(
        self,
        fixture: JsonObject,
        *,
        worker_id: str,
        now: datetime,
    ) -> PaperBarFixtureReceipt:
        self.calls.append("fixture")
        assert worker_id == WORKER_ID
        assert now.tzinfo is not None
        return PaperBarFixtureReceipt(
            series_id=SERIES_ID,
            fixture_set_id=FIXTURE_ID,
            batch_sequence=1,
            bar_count=1,
            fixture_sha256=SHA_A,
            idempotent=False,
        )

    async def enqueue_candidate(
        self,
        candidate: JsonObject,
        *,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        now: datetime,
    ) -> PaperCandidateReceipt:
        self.calls.append("candidate")
        self.fencing_token = fencing_token
        self.control_epoch = control_epoch
        assert worker_id == WORKER_ID
        assert now.tzinfo is not None
        return PaperCandidateReceipt(
            command_id=COMMAND_ID,
            intent_id=INTENT_ID,
            state="pending",
            source_revision=1,
            semantic_key_sha256=SHA_B,
            idempotent=False,
        )


def _fixture() -> JsonObject:
    return {
        "bars": [],
        "corporate_action_evidence_sha256": "c" * 64,
        "corporate_action_status": "not_required",
        "dataset_version": "fixture-v1",
        "effective_from": "2026-07-15T00:00:00+00:00",
        "effective_until": "2026-07-15T01:00:00+00:00",
        "execution_policy_version": "paper-v1",
        "fixture_set_id": FIXTURE_ID,
        "fixture_sha256": SHA_A,
        "market_calendar_evidence_sha256": "d" * 64,
        "market_calendar_version": "calendar-v1",
        "model_version": "model-v1",
        "schema_version": 1,
        "series_id": SERIES_ID,
        "source_kind": "local_fixture",
        "symbol": "005930",
        "tick_rule_evidence_sha256": "e" * 64,
        "tick_rule_version": "tick-v1",
        "tick_size_krw": 10,
        "volume_evidence_sha256": "f" * 64,
        "volume_source": "local_fixture",
    }


def _candidate() -> JsonObject:
    return {
        "account_id": "paper-primary",
        "cash_commitment_krw": 10010,
        "cost_schedule_evidence_sha256": "1" * 64,
        "cost_schedule_version": "cost-v1",
        "decision_at": "2026-07-15T00:00:00+00:00",
        "decision_feature_sha256": "2" * 64,
        "decision_id": "00000000-0000-4000-8000-000000000006",
        "execution_policy_version": "paper-v1",
        "expires_at": "2026-07-15T01:00:00+00:00",
        "fixture_series_id": SERIES_ID,
        "intent_id": INTENT_ID,
        "limit_price_krw": 10000,
        "quantity": 1,
        "risk_evaluated_at": "2026-07-15T00:00:00+00:00",
        "risk_expires_at": "2026-07-15T00:05:00+00:00",
        "risk_input": {},
        "risk_result_id": "00000000-0000-4000-8000-000000000007",
        "schema_version": 1,
        "semantic_key_sha256": SHA_B,
        "side": "buy",
        "signal_valid_from": "2026-07-15T00:00:00+00:00",
        "signal_valid_until": "2026-07-15T01:00:00+00:00",
        "strategy_version_id": "00000000-0000-4000-8000-000000000008",
        "symbol": "005930",
    }

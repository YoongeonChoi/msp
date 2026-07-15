from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.application.ports.paper_execution_producer_port import (
    PaperBarFixtureReceipt,
    PaperCandidateReceipt,
    PaperExecutionProducerPort,
)
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import ExecutionInvariantError

_FIXTURE_KEYS = {
    "bars",
    "corporate_action_evidence_sha256",
    "corporate_action_status",
    "dataset_version",
    "effective_from",
    "effective_until",
    "execution_policy_version",
    "fixture_set_id",
    "fixture_sha256",
    "market_calendar_evidence_sha256",
    "market_calendar_version",
    "model_version",
    "schema_version",
    "series_id",
    "source_kind",
    "symbol",
    "tick_rule_evidence_sha256",
    "tick_rule_version",
    "tick_size_krw",
    "volume_evidence_sha256",
    "volume_source",
}
_CANDIDATE_KEYS = {
    "account_id",
    "cash_commitment_krw",
    "cost_schedule_evidence_sha256",
    "cost_schedule_version",
    "decision_at",
    "decision_feature_sha256",
    "decision_id",
    "execution_policy_version",
    "expires_at",
    "fixture_series_id",
    "intent_id",
    "limit_price_krw",
    "quantity",
    "risk_evaluated_at",
    "risk_expires_at",
    "risk_input",
    "risk_result_id",
    "schema_version",
    "semantic_key_sha256",
    "side",
    "signal_valid_from",
    "signal_valid_until",
    "strategy_version_id",
    "symbol",
}


@dataclass(frozen=True, slots=True)
class PaperExecutionPublication:
    fixture: PaperBarFixtureReceipt
    candidate: PaperCandidateReceipt


class PublishPaperExecutionSourceV1:
    """Publish one immutable evidence batch and its fenced Paper candidate.

    The fixture write is intentionally first and idempotent.  If candidate
    authorization fails, replaying the same hash-pinned publication reuses the
    fixture and retries only the transactional candidate reservation.
    """

    def __init__(
        self,
        producer: PaperExecutionProducerPort,
        *,
        account_id: str,
        worker_id: str,
    ) -> None:
        if not account_id.strip():
            raise ExecutionInvariantError("paper_publisher_account_id_is_required")
        if not worker_id.strip():
            raise ExecutionInvariantError("paper_publisher_worker_id_is_required")
        self.producer = producer
        self.account_id = account_id
        self.worker_id = worker_id

    async def publish(
        self,
        *,
        fixture: JsonObject,
        candidate: JsonObject,
        fencing_token: int,
        control_epoch: int,
        now: datetime,
    ) -> PaperExecutionPublication:
        _require_aware(now)
        _require_positive_int(fencing_token, "paper_publisher_fencing_token_is_invalid")
        _require_positive_int(control_epoch, "paper_publisher_control_epoch_is_invalid")
        _require_exact_keys(fixture, _FIXTURE_KEYS, "paper_fixture_schema_is_invalid")
        _require_exact_keys(candidate, _CANDIDATE_KEYS, "paper_candidate_schema_is_invalid")
        if fixture.get("schema_version") != 1 or candidate.get("schema_version") != 1:
            raise ExecutionInvariantError("paper_publication_schema_version_is_invalid")
        if candidate.get("account_id") != self.account_id:
            raise ExecutionInvariantError("paper_publication_account_mismatch")
        if fixture.get("series_id") != candidate.get("fixture_series_id"):
            raise ExecutionInvariantError("paper_publication_series_mismatch")
        if fixture.get("symbol") != candidate.get("symbol"):
            raise ExecutionInvariantError("paper_publication_symbol_mismatch")
        if fixture.get("execution_policy_version") != candidate.get(
            "execution_policy_version"
        ):
            raise ExecutionInvariantError("paper_publication_policy_mismatch")

        fixture_receipt = await self.producer.ingest_bar_fixture(
            fixture,
            worker_id=self.worker_id,
            now=now,
        )
        if fixture_receipt.series_id != fixture.get("series_id"):
            raise ExecutionInvariantError("paper_fixture_receipt_series_mismatch")
        if fixture_receipt.fixture_set_id != fixture.get("fixture_set_id"):
            raise ExecutionInvariantError("paper_fixture_receipt_identity_mismatch")
        if fixture_receipt.fixture_sha256 != fixture.get("fixture_sha256"):
            raise ExecutionInvariantError("paper_fixture_receipt_digest_mismatch")

        candidate_receipt = await self.producer.enqueue_candidate(
            candidate,
            worker_id=self.worker_id,
            fencing_token=fencing_token,
            control_epoch=control_epoch,
            now=now,
        )
        if candidate_receipt.intent_id != candidate.get("intent_id"):
            raise ExecutionInvariantError("paper_candidate_receipt_identity_mismatch")
        if candidate_receipt.semantic_key_sha256 != candidate.get(
            "semantic_key_sha256"
        ):
            raise ExecutionInvariantError("paper_candidate_receipt_semantic_key_mismatch")
        if candidate_receipt.state != "pending":
            raise ExecutionInvariantError("paper_candidate_receipt_state_is_invalid")
        return PaperExecutionPublication(
            fixture=fixture_receipt,
            candidate=candidate_receipt,
        )


def _require_exact_keys(
    value: JsonObject,
    expected: set[str],
    reason: str,
) -> None:
    if set(value) != expected:
        raise ExecutionInvariantError(reason)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionInvariantError("paper_publisher_clock_must_be_timezone_aware")


def _require_positive_int(value: int, reason: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionInvariantError(reason)

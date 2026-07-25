from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    persistence_authority_fingerprint,
)
from app.config import Settings
from app.domain.common.json import JsonObject, JsonValue
from app.domain.execution_v2.cash_settlement import (
    CashSettlementClaim,
    CashSettlementCompletionAmbiguousError,
    CashSettlementCompletionRetryableError,
    CashSettlementFailureCode,
    CashSettlementFailureReceipt,
    CashSettlementReceipt,
)
from app.domain.execution_v2.models import (
    AccountingTransaction,
    ExecutionEnvironment,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    ExecutionStatus,
    ObservationRecordResult,
    OrderIntentReservationResult,
    PaperExecutionCheckpoint,
    WorkerLease,
    WorkerLeaseRelease,
)
from app.domain.execution_v2.reconciliation import (
    ExecutionReconciliationClaim,
    ExecutionReconciliationCompletion,
    ExpiredPaperIntentResult,
    PreDispatchFailureResult,
    ReconciliationOutcome,
    RecoveryDisposition,
)
from app.domain.execution_v2.unknown_resolution import (
    UnknownResolutionApplicationReceipt,
    UnknownResolutionApplyAmbiguousError,
    UnknownResolutionCandidate,
    UnknownResolutionClaim,
    UnknownResolutionTerminalStatus,
    UnknownResolutionWorkState,
)
from app.domain.operations.models import (
    WORKER_APPLICABLE_OPERATION_COMMAND_TYPES,
    ClaimedDeliveryOutboxItem,
    ClaimedOperationCommand,
    CompletedOutboxDelivery,
    FailedOutboxDelivery,
    OperationCommandAcknowledgement,
    OperationCommandType,
    RecordedWorkerHeartbeat,
    WorkerHeartbeatStatus,
)
from app.infrastructure.release_metadata import worker_release_metadata
from app.infrastructure.supabase_headers import supabase_api_headers

WorkerApiRpc = Literal[
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
]

WORKER_API_SCHEMA = "worker_api"
WORKER_API_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {
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
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class SupabaseWorkerApi:
    """RPC-only adapter for the private worker execution schema.

    It deliberately exposes no table CRUD primitive. Construction itself is
    disabled unless the explicit V2 worker API runtime flag is enabled.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        release_sha: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.execution_v2_enabled or not settings.execution_v2_worker_api_enabled:
            raise ExecutionInvariantError("execution_v2_worker_api_is_not_enabled")
        supabase_url = settings.supabase_url
        if not supabase_url or settings.supabase_secret_key is None:
            raise ExecutionInvariantError("execution_v2_worker_api_credentials_are_missing")
        resolved_release_sha = release_sha or worker_release_metadata().get("release_sha")
        if (
            not isinstance(resolved_release_sha, str)
            or _SHA_RE.fullmatch(resolved_release_sha) is None
        ):
            raise ExecutionInvariantError("execution_v2_worker_api_release_sha_is_missing")
        try:
            authority = persistence_authority_fingerprint(
                namespace="supabase-worker-api",
                origin=supabase_url,
                profile=WORKER_API_SCHEMA,
            )
        except ValueError:
            raise ExecutionInvariantError(
                "execution_v2_worker_api_origin_is_invalid"
            ) from None
        secret = settings.supabase_secret_key.get_secret_value()
        self._release_sha = resolved_release_sha.lower()
        self._persistence_authority: PersistenceAuthority = authority
        self._base_url = supabase_url.rstrip("/") + "/rest/v1/rpc"
        self._headers = supabase_api_headers(secret) | {
            "accept-profile": WORKER_API_SCHEMA,
            "content-profile": WORKER_API_SCHEMA,
            "content-type": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=10.0,
            headers=self._headers,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def release_sha(self) -> str:
        return self._release_sha

    @property
    def current_release_sha(self) -> str:
        return self._release_sha

    @property
    def persistence_authority(self) -> PersistenceAuthority:
        return self._persistence_authority

    @property
    def base_url(self) -> str:
        return self._base_url

    async def acquire_worker_lease(
        self,
        *,
        account_id: str,
        holder_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        ttl_seconds = _positive_ttl_seconds(ttl)
        payload: JsonObject = {
            "p_account_id": account_id,
            "p_holder_id": holder_id,
            "p_now": _isoformat(now),
            "p_ttl_seconds": ttl_seconds,
            "p_release_sha": self.release_sha,
        }
        row = _singleton_row(await self._rpc("acquire_worker_lease", payload))
        _require_exact_keys(
            row,
            {"account_id", "holder_id", "fencing_token", "acquired_at", "expires_at"},
        )
        return _worker_lease_from_row(row)

    async def renew_worker_lease(
        self,
        lease: WorkerLease,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        ttl_seconds = _positive_ttl_seconds(ttl)
        payload: JsonObject = {
            "p_account_id": lease.account_id,
            "p_holder_id": lease.holder_id,
            "p_fencing_token": lease.fencing_token,
            "p_now": _isoformat(now),
            "p_ttl_seconds": ttl_seconds,
            "p_release_sha": self.release_sha,
        }
        row = _singleton_row(await self._rpc("renew_worker_lease", payload))
        _require_exact_keys(
            row,
            {"account_id", "holder_id", "fencing_token", "acquired_at", "expires_at"},
        )
        renewed = _worker_lease_from_row(row)
        if (
            renewed.account_id != lease.account_id
            or renewed.holder_id != lease.holder_id
            or renewed.fencing_token != lease.fencing_token
        ):
            raise ExecutionInvariantError("worker_api_renewed_lease_identity_mismatch")
        return renewed

    async def release_worker_lease(
        self,
        lease: WorkerLease,
        *,
        now: datetime,
    ) -> WorkerLeaseRelease:
        row = _singleton_row(
            await self._rpc(
                "release_worker_lease",
                {
                    "p_account_id": lease.account_id,
                    "p_holder_id": lease.holder_id,
                    "p_fencing_token": lease.fencing_token,
                    "p_now": _isoformat(now),
                    "p_release_sha": self.release_sha,
                },
            )
        )
        _require_exact_keys(
            row,
            {"account_id", "holder_id", "fencing_token", "released_at", "idempotent"},
        )
        released = WorkerLeaseRelease(
            account_id=_required_text(row, "account_id"),
            holder_id=_required_text(row, "holder_id"),
            fencing_token=_required_int(row, "fencing_token"),
            released_at=_required_datetime(row, "released_at"),
            idempotent=_required_bool(row, "idempotent"),
        )
        if (
            released.account_id != lease.account_id
            or released.holder_id != lease.holder_id
            or released.fencing_token != lease.fencing_token
        ):
            raise ExecutionInvariantError("worker_api_released_lease_identity_mismatch")
        return released

    async def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        status: WorkerHeartbeatStatus,
        details: JsonObject,
        now: datetime,
    ) -> RecordedWorkerHeartbeat:
        _require_uuid_input(worker_id, "worker_id")
        heartbeat_time = _isoformat(now)
        if status not in {"ok", "warning", "error", "shutting_down"}:
            raise ExecutionInvariantError("worker_api_heartbeat_status_is_invalid")
        if not isinstance(details, dict) or not all(isinstance(key, str) for key in details):
            raise ExecutionInvariantError("worker_api_heartbeat_details_are_invalid")
        supplied_release_sha = details.get("release_sha")
        if supplied_release_sha not in {None, self.release_sha}:
            raise ExecutionInvariantError("worker_api_heartbeat_release_sha_mismatch")
        if status == "ok":
            if details.get("checkpoint") not in {
                "cycle_completed",
                "operations_completed",
                "independent_scheduler_running",
            }:
                raise ExecutionInvariantError("worker_api_completed_heartbeat_checkpoint_invalid")
            completed_at = details.get("completed_at")
            if not isinstance(completed_at, str):
                raise ExecutionInvariantError("worker_api_completed_heartbeat_time_invalid")
            if details.get("checkpoint") == "independent_scheduler_running":
                stage_times = details.get("stage_last_completed_at")
                if not isinstance(stage_times, dict) or set(stage_times) != {
                    "commands",
                    "execution",
                    "settlement",
                    "reconciliation",
                    "outbox",
                }:
                    raise ExecutionInvariantError(
                        "worker_api_scheduler_heartbeat_stages_invalid"
                    )
                for value in stage_times.values():
                    if not isinstance(value, str):
                        raise ExecutionInvariantError(
                            "worker_api_scheduler_heartbeat_stages_invalid"
                        )
                    try:
                        stage_time = datetime.fromisoformat(value)
                    except ValueError as exc:
                        raise ExecutionInvariantError(
                            "worker_api_scheduler_heartbeat_stages_invalid"
                        ) from exc
                    if (
                        stage_time.tzinfo is None
                        or stage_time.utcoffset() is None
                        or not now - timedelta(hours=1)
                        <= stage_time
                        <= now + timedelta(seconds=30)
                    ):
                        raise ExecutionInvariantError(
                            "worker_api_scheduler_heartbeat_stages_invalid"
                        )
            try:
                completed_time = datetime.fromisoformat(completed_at)
            except ValueError as exc:
                raise ExecutionInvariantError(
                    "worker_api_completed_heartbeat_time_invalid"
                ) from exc
            if (
                completed_time.tzinfo is None
                or completed_time.utcoffset() is None
                or not now - timedelta(hours=1) <= completed_time <= now + timedelta(seconds=30)
            ):
                raise ExecutionInvariantError("worker_api_completed_heartbeat_time_invalid")
        heartbeat_details: JsonObject = dict(details)
        heartbeat_details["release_sha"] = self.release_sha
        row = _singleton_row(
            await self._rpc(
                "record_worker_heartbeat",
                {
                    "p_worker_id": worker_id,
                    "p_status": status,
                    "p_details": heartbeat_details,
                    "p_now": heartbeat_time,
                    "p_release_sha": self.release_sha,
                },
            )
        )
        _require_exact_keys(row, {"heartbeat_id", "created_at"})
        return RecordedWorkerHeartbeat(
            heartbeat_id=_required_uuid_text(row, "heartbeat_id"),
            created_at=_required_datetime(row, "created_at"),
        )

    async def reserve_order_intent(
        self,
        intent: ExecutionIntent,
    ) -> OrderIntentReservationResult:
        payload: JsonObject = {
            "p_intent_id": intent.id,
            "p_semantic_key": intent.semantic_key,
            "p_account_id": intent.account_id,
            "p_environment": intent.environment,
            "p_strategy_version_id": intent.strategy_version_id,
            "p_decision_id": intent.decision_id,
            "p_risk_result_id": intent.risk_result_id,
            "p_decision_feature_sha256": intent.decision_feature_sha256,
            "p_risk_allowed": intent.risk_allowed,
            "p_risk_reason_codes": list(intent.risk_reason_codes),
            "p_risk_evaluated_at": _isoformat(intent.risk_evaluated_at),
            "p_risk_expires_at": _isoformat(intent.risk_expires_at),
            "p_symbol": intent.symbol,
            "p_side": intent.side,
            "p_quantity": intent.quantity,
            "p_limit_price_krw": intent.limit_price_krw,
            "p_decision_at": _isoformat(intent.decision_at),
            "p_signal_valid_from": _isoformat(intent.signal_valid_from),
            "p_signal_valid_until": _isoformat(intent.signal_valid_until),
            "p_execution_policy_version": intent.execution_policy_version,
            "p_cost_schedule_version": intent.cost_schedule_version,
            "p_cost_schedule_evidence_sha256": intent.cost_schedule_evidence_sha256,
            "p_cash_commitment_krw": intent.cash_commitment_krw,
            "p_eligible_at": _isoformat(intent.eligible_at),
            "p_expires_at": _isoformat(intent.expires_at),
            "p_gate_epoch": intent.gate_epoch,
            "p_holder_id": intent.lease_holder_id,
            "p_fencing_token": intent.lease_fencing_token,
            "p_release_sha": self.release_sha,
        }
        row = _singleton_row(await self._rpc("reserve_order_intent", payload))
        _require_exact_keys(row, {"reserved", "intent_id", "reservation_id", "reason_code"})
        reserved = _required_bool(row, "reserved")
        returned_intent_id = _required_uuid_text(row, "intent_id")
        reservation_id = _required_uuid_text(row, "reservation_id")
        reason_code = _required_text(row, "reason_code")
        if reserved and reason_code == "reserved":
            if returned_intent_id != intent.id:
                raise ExecutionInvariantError("worker_api_reserved_intent_identity_mismatch")
            state: Literal["created", "existing_replay", "semantic_duplicate"] = "created"
        elif not reserved and reason_code == "duplicate_semantic_intent":
            state = (
                "existing_replay"
                if returned_intent_id == intent.id
                else "semantic_duplicate"
            )
        else:
            raise ExecutionInvariantError("worker_api_reservation_result_is_invalid")
        return OrderIntentReservationResult(
            state=state,
            intent_id=returned_intent_id,
            reservation_id=reservation_id,
            reason_code=reason_code,
        )

    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
    ) -> None:
        request_sha256 = _contract_request_sha256(intent)
        payload: JsonObject = {
            "p_intent_id": intent.id,
            "p_account_id": intent.account_id,
            "p_environment": intent.environment,
            "p_holder_id": intent.lease_holder_id,
            "p_fencing_token": intent.lease_fencing_token,
            "p_gate_epoch": intent.gate_epoch,
            "p_now": _isoformat(now),
            "p_request_sha256": request_sha256,
            "p_client_order_key": intent.semantic_key,
        }
        row = _singleton_row(await self._rpc("mark_dispatch_started", payload))
        _require_exact_keys(row, {"attempt_id", "prepared_at", "reason_code"})
        _required_uuid_text(row, "attempt_id")
        _required_datetime(row, "prepared_at")
        _optional_text(row, "reason_code")

    async def load_paper_execution_checkpoint(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
    ) -> PaperExecutionCheckpoint:
        if intent.environment != "paper":
            raise ExecutionInvariantError(
                "worker_api_paper_checkpoint_requires_paper_intent"
            )
        row = _singleton_row(
            await self._rpc(
                "load_paper_execution_checkpoint",
                {
                    "p_intent_id": intent.id,
                    "p_account_id": intent.account_id,
                    "p_holder_id": intent.lease_holder_id,
                    "p_fencing_token": intent.lease_fencing_token,
                    "p_control_epoch": intent.gate_epoch,
                    "p_release_sha": self.release_sha,
                    "p_now": _isoformat(now),
                },
            )
        )
        _require_exact_keys(
            row,
            {
                "intent_id",
                "attempt_id",
                "provider_order_id",
                "latest_sequence",
                "latest_status",
                "latest_observed_at",
                "latest_cumulative_quantity",
                "latest_cumulative_gross_krw",
                "latest_cumulative_commission_krw",
                "latest_cumulative_tax_krw",
                "observation_history_sha256",
                "expires_at",
                "intent_release_sha",
                "lease_release_sha",
                "position_cost_basis_method",
                "position_quantity_snapshot",
                "position_total_cost_krw",
                "position_cost_basis_sha256",
            },
        )
        latest_status = _optional_text(row, "latest_status")
        if latest_status is not None and latest_status not in {
            "open",
            "partial_filled",
            "filled",
            "expired",
            "canceled",
            "rejected",
            "unknown_requires_manual_check",
        }:
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        position_cost_basis_method = _optional_text(
            row,
            "position_cost_basis_method",
        )
        if position_cost_basis_method not in {
            None,
            "moving_weighted_average_v1",
        }:
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        checkpoint = PaperExecutionCheckpoint(
            intent_id=_required_uuid_text(row, "intent_id"),
            attempt_id=_required_uuid_text(row, "attempt_id"),
            provider_order_id=_optional_text(row, "provider_order_id"),
            latest_sequence=_optional_positive_int(row, "latest_sequence"),
            latest_status=cast(ExecutionStatus | None, latest_status),
            latest_observed_at=_optional_datetime(row, "latest_observed_at"),
            cumulative_quantity=_required_nonnegative_int(
                row,
                "latest_cumulative_quantity",
            ),
            cumulative_gross_krw=_required_nonnegative_int(
                row,
                "latest_cumulative_gross_krw",
            ),
            cumulative_commission_krw=_required_nonnegative_int(
                row,
                "latest_cumulative_commission_krw",
            ),
            cumulative_tax_krw=_required_nonnegative_int(
                row,
                "latest_cumulative_tax_krw",
            ),
            observation_history_sha256=_optional_sha256_text(
                row,
                "observation_history_sha256",
            ),
            expires_at=_required_datetime(row, "expires_at"),
            intent_release_sha=_required_release_sha_text(
                row,
                "intent_release_sha",
            ),
            lease_release_sha=_required_release_sha_text(
                row,
                "lease_release_sha",
            ),
            position_cost_basis_method=cast(
                Literal["moving_weighted_average_v1"] | None,
                position_cost_basis_method,
            ),
            position_quantity_snapshot=_optional_positive_int(
                row,
                "position_quantity_snapshot",
            ),
            position_total_cost_krw=_optional_positive_int(
                row,
                "position_total_cost_krw",
            ),
            position_cost_basis_sha256=_optional_sha256_text(
                row,
                "position_cost_basis_sha256",
            ),
        )
        if (
            checkpoint.intent_id != intent.id
            or checkpoint.expires_at != intent.expires_at
            or checkpoint.intent_release_sha != self.release_sha
            or checkpoint.lease_release_sha != self.release_sha
        ):
            raise ExecutionInvariantError(
                "worker_api_paper_checkpoint_identity_mismatch"
            )
        return checkpoint

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        intent_release_sha: str | None = None,
        now: datetime,
    ) -> ObservationRecordResult:
        if intent_release_sha != self.release_sha:
            raise ExecutionInvariantError(
                "worker_api_observation_origin_release_mismatch"
            )
        if observation.intent_id != intent.id:
            raise ExecutionInvariantError("worker_api_observation_intent_mismatch")
        if observation.observed_at > now:
            raise ExecutionInvariantError("worker_api_observation_is_from_future")
        has_fill = observation.last_fill_quantity is not None
        if has_fill != (accounting_transaction is not None):
            raise ExecutionInvariantError(
                "worker_api_fill_observation_requires_accounting_transaction"
            )
        postings: list[JsonValue] | None = None
        if accounting_transaction is not None:
            if (
                accounting_transaction.intent_id != intent.id
                or accounting_transaction.observation_sequence != observation.sequence
                or accounting_transaction.posted_at != observation.observed_at
            ):
                raise ExecutionInvariantError(
                    "worker_api_accounting_transaction_identity_mismatch"
                )
            postings = [
                {
                    "account": posting.account,
                    "debit_krw": posting.debit_krw,
                    "credit_krw": posting.credit_krw,
                }
                for posting in accounting_transaction.postings
            ]
        payload: JsonObject = {
            "p_intent_id": intent.id,
            "p_sequence": observation.sequence,
            "p_status": observation.status,
            "p_observed_at": _isoformat(observation.observed_at),
            "p_cumulative_quantity": observation.cumulative_quantity,
            "p_cumulative_gross_krw": observation.cumulative_gross_krw,
            "p_cumulative_commission_krw": observation.cumulative_commission_krw,
            "p_cumulative_tax_krw": observation.cumulative_tax_krw,
            "p_last_fill_quantity": observation.last_fill_quantity,
            "p_last_fill_price_krw": observation.last_fill_price_krw,
            "p_last_fill_settlement_date": (
                observation.last_fill_settlement_date.isoformat()
                if observation.last_fill_settlement_date is not None
                else None
            ),
            "p_reason_code": observation.reason,
            "p_provider_order_id": observation.provider_order_id,
            "p_provider_execution_id": observation.provider_execution_id,
            "p_provider_observation_sha256": observation.provider_observation_sha256,
            "p_holder_id": intent.lease_holder_id,
            "p_fencing_token": intent.lease_fencing_token,
            "p_accounting_postings": postings,
        }
        row = _singleton_row(await self._rpc("record_execution_observation", payload))
        _require_exact_keys(
            row,
            {"observation_id", "inserted", "quarantined", "reason_code"},
        )
        return ObservationRecordResult(
            observation_id=_required_uuid_text(row, "observation_id"),
            inserted=_required_bool(row, "inserted"),
            quarantined=_required_bool(row, "quarantined"),
            reason_code=_required_text(row, "reason_code"),
        )

    async def claim_execution_reconciliation_batch(
        self,
        *,
        account_id: str,
        worker_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        after_priority: int | None,
        after_intent_id: str | None,
        lease_ttl: timedelta,
    ) -> tuple[ExecutionReconciliationClaim, ...]:
        _require_nonempty_input_text(account_id, "account_id")
        _require_uuid_input(worker_id, "worker_id")
        if release_sha != self.release_sha:
            raise ExecutionInvariantError("worker_api_release_sha_mismatch")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ExecutionInvariantError(
                "worker_api_reconciliation_fencing_token_is_invalid"
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ExecutionInvariantError("worker_api_reconciliation_limit_is_invalid")
        if (after_priority is None) != (after_intent_id is None):
            raise ExecutionInvariantError("worker_api_reconciliation_cursor_is_partial")
        if after_priority is not None and (
            isinstance(after_priority, bool) or not isinstance(after_priority, int)
        ):
            raise ExecutionInvariantError("worker_api_reconciliation_priority_is_invalid")
        if after_intent_id is not None:
            _require_uuid_input(after_intent_id, "after_intent_id")
        lease_seconds = _positive_ttl_seconds(lease_ttl)
        if not 5 <= lease_seconds <= 300:
            raise ExecutionInvariantError(
                "worker_api_reconciliation_lease_ttl_is_invalid"
            )
        rows = _row_set(
            await self._rpc(
                "claim_execution_reconciliation_batch",
                {
                    "p_account_id": account_id,
                    "p_worker_id": worker_id,
                    "p_now": _isoformat(now),
                    "p_limit": limit,
                    "p_after_priority": after_priority,
                    "p_after_intent_id": after_intent_id,
                    "p_lease_seconds": lease_seconds,
                    "p_release_sha": self.release_sha,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        expected = {
            "intent_id",
            "attempt_id",
            "provider_order_id",
            "latest_observation_id",
            "latest_sequence",
            "latest_status",
            "latest_observed_at",
            "latest_cumulative_quantity",
            "latest_cumulative_gross_krw",
            "latest_cumulative_commission_krw",
            "latest_cumulative_tax_krw",
            "observation_history_sha256",
            "environment",
            "account_id",
            "symbol",
            "side",
            "quantity",
            "limit_price_krw",
            "eligible_at",
            "semantic_key_sha256",
            "decision_id",
            "risk_result_id",
            "execution_policy_version",
            "cost_schedule_version",
            "risk_policy_sha256",
            "provider_contract_version",
            "provider_openapi_sha256",
            "position_cost_basis_method",
            "position_quantity_snapshot",
            "position_average_cost_krw",
            "position_total_cost_krw",
            "position_projection_version",
            "position_cost_basis_sha256",
            "lease_fencing_token",
            "reservation_fencing_token",
            "control_epoch",
            "reservation_control_epoch",
            "intent_release_sha",
            "lease_release_sha",
            "recovery_disposition",
            "expires_at",
            "priority",
            "next_reconcile_at",
            "lease_expires_at",
        }
        claims: list[ExecutionReconciliationClaim] = []
        for row in rows:
            _require_exact_keys(row, expected)
            environment = _required_text(row, "environment")
            side = _required_text(row, "side")
            latest_status = _optional_text(row, "latest_status")
            position_cost_basis_method = _optional_text(
                row,
                "position_cost_basis_method",
            )
            recovery_disposition = _required_text(row, "recovery_disposition")
            if environment not in {"paper", "contract_test"}:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            if side not in {"buy", "sell"}:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            if latest_status is not None and latest_status not in {
                "open",
                "partial_filled",
                "filled",
                "expired",
                "canceled",
                "rejected",
                "failed_pre_dispatch",
                "unknown_requires_manual_check",
            }:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            if position_cost_basis_method not in {
                None,
                "moving_weighted_average_v1",
            }:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            if recovery_disposition not in {
                "same_release",
                "pre_dispatch_release_takeover",
                "manual_release_takeover",
            }:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            claims.append(
                ExecutionReconciliationClaim(
                    intent_id=_required_uuid_text(row, "intent_id"),
                    attempt_id=_optional_uuid_text(row, "attempt_id"),
                    provider_order_id=_optional_text(row, "provider_order_id"),
                    latest_observation_id=_optional_uuid_text(
                        row,
                        "latest_observation_id",
                    ),
                    latest_sequence=_optional_int(row, "latest_sequence"),
                    latest_status=cast(ExecutionStatus | None, latest_status),
                    latest_observed_at=_optional_datetime(
                        row,
                        "latest_observed_at",
                    ),
                    latest_cumulative_quantity=_optional_nonnegative_int(
                        row,
                        "latest_cumulative_quantity",
                    ),
                    latest_cumulative_gross_krw=_optional_nonnegative_int(
                        row,
                        "latest_cumulative_gross_krw",
                    ),
                    latest_cumulative_commission_krw=_optional_nonnegative_int(
                        row,
                        "latest_cumulative_commission_krw",
                    ),
                    latest_cumulative_tax_krw=_optional_nonnegative_int(
                        row,
                        "latest_cumulative_tax_krw",
                    ),
                    observation_history_sha256=_optional_sha256_text(
                        row,
                        "observation_history_sha256",
                    ),
                    environment=cast(Literal["paper", "contract_test"], environment),
                    account_id=_required_text(row, "account_id"),
                    symbol=_required_text(row, "symbol"),
                    side=cast(Literal["buy", "sell"], side),
                    quantity=_required_positive_int(row, "quantity"),
                    limit_price_krw=_required_positive_int(
                        row,
                        "limit_price_krw",
                    ),
                    semantic_key_sha256=_required_sha256_text(
                        row,
                        "semantic_key_sha256",
                    ),
                    decision_id=_required_uuid_text(row, "decision_id"),
                    risk_result_id=_required_uuid_text(row, "risk_result_id"),
                    execution_policy_version=_required_text(
                        row,
                        "execution_policy_version",
                    ),
                    cost_schedule_version=_required_text(
                        row,
                        "cost_schedule_version",
                    ),
                    risk_policy_sha256=_required_sha256_text(
                        row,
                        "risk_policy_sha256",
                    ),
                    provider_contract_version=_optional_text(
                        row,
                        "provider_contract_version",
                    ),
                    provider_openapi_sha256=_optional_sha256_text(
                        row,
                        "provider_openapi_sha256",
                    ),
                    position_cost_basis_method=cast(
                        Literal["moving_weighted_average_v1"] | None,
                        position_cost_basis_method,
                    ),
                    position_quantity_snapshot=_optional_positive_int(
                        row,
                        "position_quantity_snapshot",
                    ),
                    position_average_cost_krw=_optional_text(
                        row,
                        "position_average_cost_krw",
                    ),
                    position_total_cost_krw=_optional_positive_int(
                        row,
                        "position_total_cost_krw",
                    ),
                    position_projection_version=_optional_positive_int(
                        row,
                        "position_projection_version",
                    ),
                    position_cost_basis_sha256=_optional_sha256_text(
                        row,
                        "position_cost_basis_sha256",
                    ),
                    lease_fencing_token=_required_positive_int(
                        row,
                        "lease_fencing_token",
                    ),
                    reservation_fencing_token=_required_positive_int(
                        row,
                        "reservation_fencing_token",
                    ),
                    control_epoch=_required_positive_int(row, "control_epoch"),
                    reservation_control_epoch=_required_positive_int(
                        row,
                        "reservation_control_epoch",
                    ),
                    intent_release_sha=_required_release_sha_text(
                        row,
                        "intent_release_sha",
                    ),
                    lease_release_sha=_required_release_sha_text(
                        row,
                        "lease_release_sha",
                    ),
                    recovery_disposition=cast(
                        RecoveryDisposition,
                        recovery_disposition,
                    ),
                    eligible_at=_required_datetime(row, "eligible_at"),
                    expires_at=_required_datetime(row, "expires_at"),
                    priority=_required_int(row, "priority"),
                    next_reconcile_at=_required_datetime(row, "next_reconcile_at"),
                    lease_expires_at=_required_datetime(row, "lease_expires_at"),
                )
            )
            if claims[-1].lease_release_sha != self.release_sha:
                raise ExecutionInvariantError(
                    "worker_api_reconciliation_lease_release_mismatch"
                )
        return tuple(claims)

    async def fail_reserved_intent_pre_dispatch(
        self,
        *,
        intent_id: str,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        release_sha: str,
        now: datetime,
        reason_code: str,
    ) -> PreDispatchFailureResult:
        _require_uuid_input(intent_id, "intent_id")
        _require_uuid_input(worker_id, "worker_id")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ExecutionInvariantError("worker_api_fencing_token_is_invalid")
        if (
            isinstance(control_epoch, bool)
            or not isinstance(control_epoch, int)
            or control_epoch <= 0
        ):
            raise ExecutionInvariantError("worker_api_control_epoch_is_invalid")
        if release_sha != self.release_sha:
            raise ExecutionInvariantError("worker_api_release_sha_mismatch")
        _require_nonempty_input_text(reason_code, "reason_code")
        if len(reason_code) > 120:
            raise ExecutionInvariantError("worker_api_reason_code_is_invalid")
        row = _singleton_row(
            await self._rpc(
                "fail_reserved_intent_pre_dispatch",
                {
                    "p_intent_id": intent_id,
                    "p_worker_id": worker_id,
                    "p_fencing_token": fencing_token,
                    "p_control_epoch": control_epoch,
                    "p_release_sha": release_sha,
                    "p_now": _isoformat(now),
                    "p_reason_code": reason_code,
                },
            )
        )
        _require_exact_keys(
            row,
            {"intent_id", "observation_id", "state", "reason_code", "idempotent"},
        )
        state = _required_text(row, "state")
        if state != "complete":
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        result = PreDispatchFailureResult(
            intent_id=_required_uuid_text(row, "intent_id"),
            observation_id=_required_uuid_text(row, "observation_id"),
            state="complete",
            reason_code=_required_text(row, "reason_code"),
            idempotent=_required_bool(row, "idempotent"),
        )
        if result.intent_id != intent_id or result.reason_code != reason_code:
            raise ExecutionInvariantError(
                "worker_api_pre_dispatch_failure_identity_mismatch"
            )
        return result

    async def expire_paper_intent_remainder(
        self,
        *,
        intent_id: str,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        release_sha: str,
        now: datetime,
        reason_code: str,
    ) -> ExpiredPaperIntentResult:
        _require_uuid_input(intent_id, "intent_id")
        _require_uuid_input(worker_id, "worker_id")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ExecutionInvariantError("worker_api_fencing_token_is_invalid")
        if (
            isinstance(control_epoch, bool)
            or not isinstance(control_epoch, int)
            or control_epoch <= 0
        ):
            raise ExecutionInvariantError("worker_api_control_epoch_is_invalid")
        if release_sha != self.release_sha:
            raise ExecutionInvariantError("worker_api_release_sha_mismatch")
        _require_nonempty_input_text(reason_code, "reason_code")
        if len(reason_code) > 120:
            raise ExecutionInvariantError("worker_api_reason_code_is_invalid")
        row = _singleton_row(
            await self._rpc(
                "expire_paper_intent_remainder",
                {
                    "p_intent_id": intent_id,
                    "p_worker_id": worker_id,
                    "p_fencing_token": fencing_token,
                    "p_control_epoch": control_epoch,
                    "p_release_sha": release_sha,
                    "p_now": _isoformat(now),
                    "p_reason_code": reason_code,
                },
            )
        )
        _require_exact_keys(
            row,
            {
                "intent_id",
                "observation_id",
                "sequence",
                "state",
                "reason_code",
                "idempotent",
            },
        )
        state = _required_text(row, "state")
        if state != "complete":
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        result = ExpiredPaperIntentResult(
            intent_id=_required_uuid_text(row, "intent_id"),
            observation_id=_required_uuid_text(row, "observation_id"),
            sequence=_required_positive_int(row, "sequence"),
            state="complete",
            reason_code=_required_text(row, "reason_code"),
            idempotent=_required_bool(row, "idempotent"),
        )
        if result.intent_id != intent_id or result.reason_code != reason_code:
            raise ExecutionInvariantError(
                "worker_api_paper_expiry_identity_mismatch"
            )
        return result

    async def complete_execution_reconciliation(
        self,
        *,
        intent_id: str,
        worker_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        outcome: ReconciliationOutcome,
        next_reconcile_at: datetime | None,
        reason_code: str,
    ) -> ExecutionReconciliationCompletion:
        _require_uuid_input(intent_id, "intent_id")
        _require_uuid_input(worker_id, "worker_id")
        if release_sha != self.release_sha:
            raise ExecutionInvariantError("worker_api_release_sha_mismatch")
        if not isinstance(fencing_token, int) or isinstance(fencing_token, bool) \
                or fencing_token <= 0:
            raise ExecutionInvariantError(
                "worker_api_reconciliation_fencing_token_is_invalid"
            )
        if outcome not in {"reschedule", "complete", "manual"}:
            raise ExecutionInvariantError("worker_api_reconciliation_outcome_is_invalid")
        if (outcome == "reschedule") != (next_reconcile_at is not None):
            raise ExecutionInvariantError(
                "worker_api_reconciliation_schedule_is_invalid"
            )
        _require_nonempty_input_text(reason_code, "reason_code")
        if len(reason_code) > 120:
            raise ExecutionInvariantError("worker_api_reason_code_is_invalid")
        row = _singleton_row(
            await self._rpc(
                "complete_execution_reconciliation",
                {
                    "p_intent_id": intent_id,
                    "p_worker_id": worker_id,
                    "p_release_sha": release_sha,
                    "p_fencing_token": fencing_token,
                    "p_now": _isoformat(now),
                    "p_outcome": outcome,
                    "p_next_reconcile_at": (
                        _isoformat(next_reconcile_at)
                        if next_reconcile_at is not None
                        else None
                    ),
                    "p_reason_code": reason_code,
                },
            )
        )
        _require_exact_keys(row, {"intent_id", "state", "next_reconcile_at"})
        state = _required_text(row, "state")
        if state not in {"pending", "complete", "manual"}:
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        return ExecutionReconciliationCompletion(
            intent_id=_required_uuid_text(row, "intent_id"),
            state=cast(Literal["pending", "complete", "manual"], state),
            next_reconcile_at=_optional_datetime(row, "next_reconcile_at"),
        )

    async def claim_delivery_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        lease_ttl: timedelta,
    ) -> tuple[ClaimedDeliveryOutboxItem, ...]:
        _require_nonempty_input_text(worker_id, "worker_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ExecutionInvariantError("worker_api_outbox_claim_limit_is_invalid")
        lease_seconds = _positive_ttl_seconds(lease_ttl)
        if not 5 <= lease_seconds <= 300:
            raise ExecutionInvariantError("worker_api_outbox_lease_ttl_is_invalid")
        rows = _row_set(
            await self._rpc(
                "claim_delivery_outbox",
                {
                    "p_worker_id": worker_id,
                    "p_now": _isoformat(now),
                    "p_limit": limit,
                    "p_lease_seconds": lease_seconds,
                },
            )
        )
        expected = {
            "outbox_id",
            "dedupe_key",
            "event_type",
            "payload_version",
            "aggregate_type",
            "aggregate_id",
            "payload",
            "destination_type",
            "attempt_count",
            "lease_token",
            "lease_expires_at",
        }
        claimed: list[ClaimedDeliveryOutboxItem] = []
        for row in rows:
            _require_exact_keys(row, expected)
            payload = row.get("payload")
            if not isinstance(payload, dict) or not all(
                isinstance(key, str) for key in payload
            ):
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            claimed.append(
                ClaimedDeliveryOutboxItem(
                    outbox_id=_required_uuid_text(row, "outbox_id"),
                    dedupe_key=_required_text(row, "dedupe_key"),
                    event_type=_required_text(row, "event_type"),
                    payload_version=_required_positive_int(row, "payload_version"),
                    aggregate_type=_required_text(row, "aggregate_type"),
                    aggregate_id=_required_text(row, "aggregate_id"),
                    payload=payload,
                    destination_type=_required_text(row, "destination_type"),
                    attempt_count=_required_positive_int(row, "attempt_count"),
                    lease_token=_required_uuid_text(row, "lease_token"),
                    lease_expires_at=_required_datetime(row, "lease_expires_at"),
                )
            )
        return tuple(claimed)

    async def complete_outbox_delivery(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
        external_receipt_id: str,
        external_receipt_sha256: str,
    ) -> CompletedOutboxDelivery:
        _require_uuid_input(outbox_id, "outbox_id")
        _require_nonempty_input_text(worker_id, "worker_id")
        _require_uuid_input(lease_token, "lease_token")
        _require_nonempty_input_text(external_receipt_id, "external_receipt_id")
        _require_sha256_input(external_receipt_sha256, "external_receipt_sha256")
        row = _singleton_row(
            await self._rpc(
                "complete_outbox_delivery",
                {
                    "p_outbox_id": outbox_id,
                    "p_worker_id": worker_id,
                    "p_lease_token": lease_token,
                    "p_now": _isoformat(now),
                    "p_external_receipt_id": external_receipt_id,
                    "p_external_receipt_sha256": external_receipt_sha256,
                },
            )
        )
        _require_exact_keys(row, {"outbox_id", "status", "delivered_at"})
        if _required_text(row, "status") != "delivered":
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        return CompletedOutboxDelivery(
            outbox_id=_required_uuid_text(row, "outbox_id"),
            status="delivered",
            delivered_at=_required_datetime(row, "delivered_at"),
        )

    async def fail_outbox_delivery(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        retry_after: timedelta,
    ) -> FailedOutboxDelivery:
        _require_uuid_input(outbox_id, "outbox_id")
        _require_nonempty_input_text(worker_id, "worker_id")
        _require_uuid_input(lease_token, "lease_token")
        _require_nonempty_input_text(error_code, "error_code")
        if len(error_code) > 120:
            raise ExecutionInvariantError("worker_api_outbox_error_code_is_invalid")
        retry_after_seconds = _positive_ttl_seconds(retry_after)
        if retry_after_seconds > 86_400:
            raise ExecutionInvariantError("worker_api_outbox_retry_delay_is_invalid")
        row = _singleton_row(
            await self._rpc(
                "fail_outbox_delivery",
                {
                    "p_outbox_id": outbox_id,
                    "p_worker_id": worker_id,
                    "p_lease_token": lease_token,
                    "p_now": _isoformat(now),
                    "p_error_code": error_code,
                    "p_retry_after_seconds": retry_after_seconds,
                },
            )
        )
        _require_exact_keys(row, {"outbox_id", "status", "available_at"})
        status = _required_text(row, "status")
        if status not in {"pending", "dead_letter"}:
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        return FailedOutboxDelivery(
            outbox_id=_required_uuid_text(row, "outbox_id"),
            status=cast(Literal["pending", "dead_letter"], status),
            available_at=_required_datetime(row, "available_at"),
        )

    async def claim_operation_command_batch(
        self,
        *,
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
    ) -> tuple[ClaimedOperationCommand, ...]:
        _require_nonempty_input_text(account_id, "account_id")
        _require_uuid_input(holder_id, "holder_id")
        if release_sha != self.release_sha:
            raise ExecutionInvariantError("worker_api_release_sha_mismatch")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ExecutionInvariantError("worker_api_command_fencing_token_is_invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ExecutionInvariantError("worker_api_command_claim_limit_is_invalid")
        rows = _row_set(
            await self._rpc(
                "claim_operation_command_batch",
                {
                    "p_account_id": account_id,
                    "p_holder_id": holder_id,
                    "p_release_sha": self.release_sha,
                    "p_fencing_token": fencing_token,
                    "p_now": _isoformat(now),
                    "p_limit": limit,
                },
            )
        )
        expected = {
            "command_id",
            "command_type",
            "environment",
            "account_id",
            "requested_change",
            "requested_at",
            "expires_at",
            "revision",
            "claimed_at",
            "claim_expires_at",
        }
        claimed: list[ClaimedOperationCommand] = []
        for row in rows:
            _require_exact_keys(row, expected)
            command_type = _required_text(row, "command_type")
            environment = _required_text(row, "environment")
            requested_change = row.get("requested_change")
            if command_type not in WORKER_APPLICABLE_OPERATION_COMMAND_TYPES or environment not in {
                "paper",
                "contract_test",
            }:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            if not isinstance(requested_change, dict) or not all(
                isinstance(key, str) for key in requested_change
            ):
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            claimed.append(
                ClaimedOperationCommand(
                    command_id=_required_uuid_text(row, "command_id"),
                    command_type=cast(OperationCommandType, command_type),
                    environment=cast(
                        Literal["paper", "contract_test"],
                        environment,
                    ),
                    account_id=_required_text(row, "account_id"),
                    requested_change=requested_change,
                    requested_at=_required_datetime(row, "requested_at"),
                    expires_at=_required_datetime(row, "expires_at"),
                    revision=_required_positive_int(row, "revision"),
                    claimed_at=_required_datetime(row, "claimed_at"),
                    claim_expires_at=_required_datetime(row, "claim_expires_at"),
                )
            )
        return tuple(claimed)

    async def acknowledge_operation_command(
        self,
        *,
        command_id: str,
        phase: Literal["applied", "failed"],
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        expected_revision: int,
        now: datetime,
        result_summary: JsonObject,
        failure_code: str | None = None,
    ) -> OperationCommandAcknowledgement:
        _require_uuid_input(command_id, "command_id")
        if phase not in {"applied", "failed"}:
            raise ExecutionInvariantError("worker_api_command_phase_is_invalid")
        _require_nonempty_input_text(account_id, "account_id")
        _require_uuid_input(holder_id, "holder_id")
        if release_sha != self.release_sha:
            raise ExecutionInvariantError("worker_api_release_sha_mismatch")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ExecutionInvariantError("worker_api_command_fencing_token_is_invalid")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise ExecutionInvariantError("worker_api_command_revision_is_invalid")
        if not isinstance(result_summary, dict) or not all(
            isinstance(key, str) for key in result_summary
        ):
            raise ExecutionInvariantError("worker_api_command_result_summary_is_invalid")
        if phase == "failed":
            if failure_code is None:
                raise ExecutionInvariantError("worker_api_command_failure_code_is_required")
            _require_nonempty_input_text(failure_code, "failure_code")
        elif failure_code is not None:
            raise ExecutionInvariantError("worker_api_command_failure_code_is_unexpected")
        row = _singleton_row(
            await self._rpc(
                "acknowledge_operation_command",
                {
                    "p_command_id": command_id,
                    "p_phase": phase,
                    "p_account_id": account_id,
                    "p_holder_id": holder_id,
                    "p_release_sha": self.release_sha,
                    "p_fencing_token": fencing_token,
                    "p_expected_revision": expected_revision,
                    "p_now": _isoformat(now),
                    "p_result_summary": result_summary,
                    "p_failure_code": failure_code,
                },
            )
        )
        _require_exact_keys(
            row,
            {
                "command_id",
                "state",
                "claimed_at",
                "applied_at",
                "post_control_epoch",
                "failure_code",
            },
        )
        state = _required_text(row, "state")
        if state != phase:
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        return OperationCommandAcknowledgement(
            command_id=_required_uuid_text(row, "command_id"),
            state=cast(Literal["claimed", "applied", "failed"], state),
            claimed_at=_optional_datetime(row, "claimed_at"),
            applied_at=_optional_datetime(row, "applied_at"),
            post_control_epoch=_optional_int(row, "post_control_epoch"),
            failure_code=_optional_text(row, "failure_code"),
        )

    async def list_unknown_resolution_candidates(
        self,
        *,
        account_id: str,
        environment: ExecutionEnvironment,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
    ) -> tuple[UnknownResolutionCandidate, ...]:
        _validate_unknown_resolution_actor(
            account_id=account_id,
            environment=environment,
            holder_id=holder_id,
            release_sha=release_sha,
            expected_release_sha=self.release_sha,
            fencing_token=fencing_token,
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
            raise ExecutionInvariantError("worker_api_unknown_resolution_limit_is_invalid")
        rows = _row_set(
            await self._unknown_resolution_rpc(
                "list_unknown_resolution_v2",
                {
                    "account_id": account_id,
                    "holder_id": holder_id,
                    "release_sha": release_sha,
                    "fencing_token": fencing_token,
                    "result_limit": limit,
                    "observed_at": _isoformat(now),
                },
            )
        )
        expected_keys = {
            "command_id",
            "request_id",
            "review_id",
            "break_id",
            "intent_id",
            "terminal_status",
            "request_payload_sha256",
            "review_payload_sha256",
            "command_revision",
            "work_revision",
            "work_state",
            "claim_token",
            "claim_expires_at",
            "expected_control_epoch",
        }
        candidates: list[UnknownResolutionCandidate] = []
        for row in rows:
            _require_exact_keys(row, expected_keys)
            terminal_status = _required_text(row, "terminal_status")
            work_state = _required_text(row, "work_state")
            if terminal_status not in {"filled", "canceled", "expired", "rejected"}:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            if work_state not in {"approved", "claimed"}:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            candidates.append(
                UnknownResolutionCandidate(
                    command_id=_required_uuid_text(row, "command_id"),
                    request_id=_required_uuid_text(row, "request_id"),
                    review_id=_required_uuid_text(row, "review_id"),
                    break_id=_required_uuid_text(row, "break_id"),
                    intent_id=_required_uuid_text(row, "intent_id"),
                    terminal_status=cast(
                        UnknownResolutionTerminalStatus,
                        terminal_status,
                    ),
                    request_payload_sha256=_required_sha256_text(
                        row,
                        "request_payload_sha256",
                    ),
                    review_payload_sha256=_required_sha256_text(
                        row,
                        "review_payload_sha256",
                    ),
                    command_revision=_required_nonnegative_int(
                        row,
                        "command_revision",
                    ),
                    work_revision=_required_nonnegative_int(row, "work_revision"),
                    work_state=cast(UnknownResolutionWorkState, work_state),
                    claim_token=_optional_uuid_text(row, "claim_token"),
                    claim_expires_at=_optional_datetime(row, "claim_expires_at"),
                    expected_control_epoch=_required_positive_int(
                        row,
                        "expected_control_epoch",
                    ),
                    account_id=account_id,
                    environment=environment,
                    holder_id=holder_id,
                    release_sha=release_sha,
                    lease_fencing_token=fencing_token,
                )
            )
        return tuple(candidates)

    async def claim_unknown_resolution(
        self,
        candidate: UnknownResolutionCandidate,
        *,
        now: datetime,
    ) -> UnknownResolutionClaim:
        _validate_unknown_resolution_actor(
            account_id=candidate.account_id,
            environment=candidate.environment,
            holder_id=candidate.holder_id,
            release_sha=candidate.release_sha,
            expected_release_sha=self.release_sha,
            fencing_token=candidate.lease_fencing_token,
        )
        row = _singleton_row(
            await self._unknown_resolution_rpc(
                "claim_unknown_resolution_v2",
                {
                    "command_id": candidate.command_id,
                    "holder_id": candidate.holder_id,
                    "release_sha": candidate.release_sha,
                    "fencing_token": candidate.lease_fencing_token,
                    "expected_command_revision": candidate.command_revision,
                    "expected_work_revision": candidate.work_revision,
                    "claimed_at": _isoformat(now),
                },
            )
        )
        _require_exact_keys(
            row,
            {
                "command_id",
                "claim_token",
                "command_revision",
                "work_revision",
                "claim_expires_at",
            },
        )
        claim = UnknownResolutionClaim(
            command_id=_required_uuid_text(row, "command_id"),
            request_id=candidate.request_id,
            review_id=candidate.review_id,
            break_id=candidate.break_id,
            intent_id=candidate.intent_id,
            terminal_status=candidate.terminal_status,
            request_payload_sha256=candidate.request_payload_sha256,
            review_payload_sha256=candidate.review_payload_sha256,
            command_revision=_required_nonnegative_int(row, "command_revision"),
            work_revision=_required_nonnegative_int(row, "work_revision"),
            claim_token=_required_uuid_text(row, "claim_token"),
            claim_expires_at=_required_datetime(row, "claim_expires_at"),
            expected_control_epoch=candidate.expected_control_epoch,
            account_id=candidate.account_id,
            environment=candidate.environment,
            holder_id=candidate.holder_id,
            release_sha=candidate.release_sha,
            lease_fencing_token=candidate.lease_fencing_token,
        )
        if (
            claim.command_id != candidate.command_id
            or claim.command_revision != candidate.command_revision + 1
            or claim.work_revision != candidate.work_revision + 1
            or claim.claim_expires_at <= now
        ):
            raise ExecutionInvariantError(
                "worker_api_unknown_resolution_claim_postcondition_mismatch"
            )
        return claim

    async def apply_unknown_resolution(
        self,
        claim: UnknownResolutionClaim,
        *,
        now: datetime,
        replay: bool,
    ) -> UnknownResolutionApplicationReceipt:
        _validate_unknown_resolution_actor(
            account_id=claim.account_id,
            environment=claim.environment,
            holder_id=claim.holder_id,
            release_sha=claim.release_sha,
            expected_release_sha=self.release_sha,
            fencing_token=claim.lease_fencing_token,
        )
        if not isinstance(replay, bool):
            raise ExecutionInvariantError(
                "worker_api_unknown_resolution_replay_flag_is_invalid"
            )
        if not replay and claim.claim_expires_at <= now:
            raise ExecutionInvariantError("worker_api_unknown_resolution_claim_is_expired")
        expected_command_revision = claim.command_revision + (1 if replay else 0)
        expected_work_revision = claim.work_revision + (1 if replay else 0)
        raw_result = await self._unknown_resolution_rpc(
            "apply_unknown_resolution_v2",
            {
                "command_id": claim.command_id,
                "claim_token": claim.claim_token,
                "holder_id": claim.holder_id,
                "release_sha": claim.release_sha,
                "fencing_token": claim.lease_fencing_token,
                "expected_command_revision": expected_command_revision,
                "expected_work_revision": expected_work_revision,
                "expected_control_epoch": claim.expected_control_epoch,
                "applied_at": _isoformat(now),
            },
        )
        try:
            row = _singleton_row(raw_result)
            _require_exact_keys(
                row,
                {
                    "schema_version",
                    "command_id",
                    "break_id",
                    "intent_id",
                    "state",
                    "receipt_revision",
                    "break_revision",
                    "request_digest_sha256",
                    "review_digest_sha256",
                    "terminal_status",
                    "claim_token",
                    "work_revision",
                    "application_id",
                    "application_sha256",
                    "accounting_mutation_allowed",
                    "resolution_complete",
                    "inserted",
                },
            )
            state = _required_text(row, "state")
            terminal_status = _required_text(row, "terminal_status")
            if state != "applied" or terminal_status not in {
                "filled",
                "canceled",
                "expired",
                "rejected",
            }:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            receipt = UnknownResolutionApplicationReceipt(
                schema_version=cast(Literal[2], _required_int(row, "schema_version")),
                command_id=_required_uuid_text(row, "command_id"),
                break_id=_required_uuid_text(row, "break_id"),
                intent_id=_required_uuid_text(row, "intent_id"),
                state="applied",
                receipt_revision=_required_nonnegative_int(
                    row,
                    "receipt_revision",
                ),
                break_revision=_required_nonnegative_int(row, "break_revision"),
                request_digest_sha256=_required_sha256_text(
                    row,
                    "request_digest_sha256",
                ),
                review_digest_sha256=_required_sha256_text(
                    row,
                    "review_digest_sha256",
                ),
                terminal_status=cast(
                    UnknownResolutionTerminalStatus,
                    terminal_status,
                ),
                claim_token=_required_uuid_text(row, "claim_token"),
                work_revision=_required_nonnegative_int(row, "work_revision"),
                application_id=_required_uuid_text(row, "application_id"),
                application_sha256=_required_sha256_text(
                    row,
                    "application_sha256",
                ),
                accounting_mutation_allowed=cast(
                    Literal[True],
                    _required_bool(row, "accounting_mutation_allowed"),
                ),
                resolution_complete=cast(
                    Literal[True],
                    _required_bool(row, "resolution_complete"),
                ),
                inserted=_required_bool(row, "inserted"),
                account_id=claim.account_id,
                environment=claim.environment,
                holder_id=claim.holder_id,
                release_sha=claim.release_sha,
                lease_fencing_token=claim.lease_fencing_token,
                control_epoch=claim.expected_control_epoch,
            )
            if (
                receipt.command_id != claim.command_id
                or receipt.break_id != claim.break_id
                or receipt.intent_id != claim.intent_id
                or receipt.claim_token != claim.claim_token
                or receipt.request_digest_sha256 != claim.request_payload_sha256
                or receipt.review_digest_sha256 != claim.review_payload_sha256
                or receipt.terminal_status != claim.terminal_status
                or receipt.receipt_revision != claim.command_revision + 1
                or receipt.work_revision != claim.work_revision + 1
                or receipt.inserted is replay
            ):
                raise ExecutionInvariantError(
                    "worker_api_unknown_resolution_receipt_mismatch"
                )
        except ExecutionInvariantError as exc:
            raise UnknownResolutionApplyAmbiguousError() from exc
        return receipt

    async def claim_cash_settlement_batch(
        self,
        *,
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
    ) -> tuple[CashSettlementClaim, ...]:
        _require_nonempty_input_text(account_id, "cash_settlement_account_id")
        _validate_cash_settlement_actor(
            holder_id=holder_id,
            release_sha=release_sha,
            expected_release_sha=self.release_sha,
            fencing_token=fencing_token,
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ExecutionInvariantError("worker_api_cash_settlement_limit_is_invalid")
        rows = _row_set(
            await self._cash_settlement_rpc(
                "claim_cash_settlement_batch",
                {
                    "p_account_id": account_id,
                    "p_holder_id": holder_id,
                    "p_release_sha": release_sha,
                    "p_fencing_token": fencing_token,
                    "p_now": _isoformat(now),
                    "p_limit": limit,
                },
            )
        )
        claims: list[CashSettlementClaim] = []
        for row in rows:
            _require_exact_keys(
                row,
                {
                    "obligation_id",
                    "fill_id",
                    "intent_id",
                    "account_id",
                    "environment",
                    "obligation_type",
                    "amount_krw",
                    "settlement_date",
                    "obligation_sha256",
                    "revision",
                    "claim_token",
                    "claim_expires_at",
                },
            )
            environment = _required_text(row, "environment")
            obligation_type = _required_text(row, "obligation_type")
            if environment not in {"paper", "contract_test"} or obligation_type not in {
                "cash_payable",
                "cash_receivable",
            }:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            claim = CashSettlementClaim(
                obligation_id=_required_uuid_text(row, "obligation_id"),
                fill_id=_required_uuid_text(row, "fill_id"),
                intent_id=_required_uuid_text(row, "intent_id"),
                account_id=_required_text(row, "account_id"),
                environment=cast(Literal["paper", "contract_test"], environment),
                obligation_type=cast(
                    Literal["cash_payable", "cash_receivable"],
                    obligation_type,
                ),
                amount_krw=_required_positive_int(row, "amount_krw"),
                settlement_date=_required_date(row, "settlement_date"),
                obligation_sha256=_required_sha256_text(row, "obligation_sha256"),
                revision=_required_positive_int(row, "revision"),
                claim_token=_required_uuid_text(row, "claim_token"),
                claim_expires_at=_required_datetime(row, "claim_expires_at"),
            )
            if claim.account_id != account_id:
                raise ExecutionInvariantError("worker_api_cash_settlement_claim_is_invalid")
            claims.append(claim)
        return tuple(claims)

    async def complete_cash_settlement(
        self,
        claim: CashSettlementClaim,
        *,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
    ) -> CashSettlementReceipt:
        _validate_cash_settlement_actor(
            holder_id=holder_id,
            release_sha=release_sha,
            expected_release_sha=self.release_sha,
            fencing_token=fencing_token,
        )
        raw_result = await self._cash_settlement_rpc(
            "complete_cash_settlement",
            {
                "p_obligation_id": claim.obligation_id,
                "p_expected_revision": claim.revision,
                "p_claim_token": claim.claim_token,
                "p_holder_id": holder_id,
                "p_release_sha": release_sha,
                "p_fencing_token": fencing_token,
                "p_now": _isoformat(now),
            },
        )
        try:
            row = _singleton_row(raw_result)
            _require_exact_keys(
                row,
                {
                    "schema_version",
                    "obligation_id",
                    "settlement_transaction_id",
                    "claim_revision",
                    "settled_revision",
                    "obligation_type",
                    "amount_krw",
                    "settlement_date",
                    "settled_at",
                    "replayed",
                },
            )
            if _required_int(row, "schema_version") != 1:
                raise ExecutionInvariantError("worker_api_rpc_schema_version_is_invalid")
            obligation_type = _required_text(row, "obligation_type")
            if obligation_type not in {"cash_payable", "cash_receivable"}:
                raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
            receipt = CashSettlementReceipt(
                obligation_id=_required_uuid_text(row, "obligation_id"),
                settlement_transaction_id=_required_uuid_text(
                    row, "settlement_transaction_id"
                ),
                claim_revision=_required_positive_int(row, "claim_revision"),
                settled_revision=_required_positive_int(row, "settled_revision"),
                obligation_type=cast(
                    Literal["cash_payable", "cash_receivable"], obligation_type
                ),
                amount_krw=_required_positive_int(row, "amount_krw"),
                settlement_date=_required_date(row, "settlement_date"),
                settled_at=_required_datetime(row, "settled_at"),
                replayed=_required_bool(row, "replayed"),
            )
            if (
                receipt.obligation_id != claim.obligation_id
                or receipt.claim_revision != claim.revision
                or receipt.obligation_type != claim.obligation_type
                or receipt.amount_krw != claim.amount_krw
                or receipt.settlement_date != claim.settlement_date
            ):
                raise ExecutionInvariantError(
                    "worker_api_cash_settlement_receipt_mismatch"
                )
        except ExecutionInvariantError as exc:
            raise CashSettlementCompletionAmbiguousError() from exc
        return receipt

    async def fail_cash_settlement_attempt(
        self,
        claim: CashSettlementClaim,
        *,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        error_code: CashSettlementFailureCode,
    ) -> CashSettlementFailureReceipt:
        _validate_cash_settlement_actor(
            holder_id=holder_id,
            release_sha=release_sha,
            expected_release_sha=self.release_sha,
            fencing_token=fencing_token,
        )
        if error_code not in {
            "settlement_dependency_unavailable",
            "settlement_projection_conflict",
            "settlement_worker_error",
        }:
            raise ExecutionInvariantError("worker_api_cash_settlement_error_is_invalid")
        row = _singleton_row(
            await self._cash_settlement_rpc(
                "fail_cash_settlement_attempt",
                {
                    "p_obligation_id": claim.obligation_id,
                    "p_expected_revision": claim.revision,
                    "p_claim_token": claim.claim_token,
                    "p_holder_id": holder_id,
                    "p_release_sha": release_sha,
                    "p_fencing_token": fencing_token,
                    "p_now": _isoformat(now),
                    "p_error_code": error_code,
                },
            )
        )
        _require_exact_keys(
            row,
            {
                "schema_version",
                "obligation_id",
                "claim_revision",
                "revision",
                "state",
                "attempt_count",
                "available_at",
                "error_code",
                "replayed",
            },
        )
        if _required_int(row, "schema_version") != 1:
            raise ExecutionInvariantError("worker_api_rpc_schema_version_is_invalid")
        state = _required_text(row, "state")
        returned_error_code = _required_text(row, "error_code")
        if state not in {"pending", "dead_letter"} or returned_error_code not in {
            "settlement_dependency_unavailable",
            "settlement_projection_conflict",
            "settlement_worker_error",
        }:
            raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
        receipt = CashSettlementFailureReceipt(
            obligation_id=_required_uuid_text(row, "obligation_id"),
            claim_revision=_required_positive_int(row, "claim_revision"),
            revision=_required_positive_int(row, "revision"),
            state=cast(Literal["pending", "dead_letter"], state),
            attempt_count=_required_positive_int(row, "attempt_count"),
            available_at=_required_datetime(row, "available_at"),
            error_code=cast(CashSettlementFailureCode, returned_error_code),
            replayed=_required_bool(row, "replayed"),
        )
        if (
            receipt.obligation_id != claim.obligation_id
            or receipt.claim_revision != claim.revision
            or receipt.error_code != error_code
        ):
            raise ExecutionInvariantError(
                "worker_api_cash_settlement_failure_receipt_mismatch"
            )
        return receipt

    async def _cash_settlement_rpc(
        self,
        rpc: Literal[
            "claim_cash_settlement_batch",
            "complete_cash_settlement",
            "fail_cash_settlement_attempt",
        ],
        payload: JsonObject,
    ) -> object:
        try:
            response = await self._client.post(
                f"{self._base_url}/{rpc}",
                json=payload,
                headers=self._headers,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if rpc == "complete_cash_settlement":
                raise CashSettlementCompletionAmbiguousError() from exc
            raise ExecutionInvariantError(
                "worker_api_cash_settlement_transport_failed"
            ) from exc
        if response.is_error:
            message = _postgrest_error_message(response)
            if (
                rpc == "complete_cash_settlement"
                and message == "cash_settlement_projection_maturity_failed"
            ):
                raise CashSettlementCompletionRetryableError(
                    "settlement_projection_conflict"
                )
            raise ExecutionInvariantError(
                "worker_api_cash_settlement_rpc_rejected"
            )
        try:
            return response.json()
        except ValueError as exc:
            if rpc == "complete_cash_settlement":
                raise CashSettlementCompletionAmbiguousError() from exc
            raise ExecutionInvariantError(
                "worker_api_cash_settlement_response_is_invalid"
            ) from exc

    async def _unknown_resolution_rpc(
        self,
        rpc: Literal[
            "list_unknown_resolution_v2",
            "claim_unknown_resolution_v2",
            "apply_unknown_resolution_v2",
        ],
        payload: JsonObject,
    ) -> object:
        if rpc not in WORKER_API_RPC_ALLOWLIST:
            raise ExecutionInvariantError("worker_api_rpc_is_not_allowed")
        try:
            response = await self._client.post(
                f"{self._base_url}/{rpc}",
                json=payload,
                headers=self._headers,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if rpc == "apply_unknown_resolution_v2":
                raise UnknownResolutionApplyAmbiguousError() from exc
            raise ExecutionInvariantError(
                "worker_api_unknown_resolution_transport_failed"
            ) from exc
        if response.is_error:
            raise ExecutionInvariantError("worker_api_unknown_resolution_rpc_rejected")
        try:
            return response.json()
        except ValueError as exc:
            if rpc == "apply_unknown_resolution_v2":
                raise UnknownResolutionApplyAmbiguousError() from exc
            raise ExecutionInvariantError(
                "worker_api_unknown_resolution_response_is_invalid"
            ) from exc

    async def _rpc(self, rpc: WorkerApiRpc, payload: JsonObject) -> object:
        if rpc not in WORKER_API_RPC_ALLOWLIST:
            raise ExecutionInvariantError("worker_api_rpc_is_not_allowed")
        try:
            response = await self._client.post(
                f"{self._base_url}/{rpc}",
                json=payload,
                headers=self._headers,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionInvariantError("worker_api_rpc_failed_or_returned_invalid_json") from exc


def _contract_request_sha256(intent: ExecutionIntent) -> str:
    payload = {
        "amount_krw": intent.quantity * intent.limit_price_krw,
        "client_order_key": intent.semantic_key,
        "limit_price_krw": intent.limit_price_krw,
        "quantity": intent.quantity,
        "side": intent.side,
        "symbol": intent.symbol,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _positive_ttl_seconds(ttl: timedelta) -> int:
    if not isinstance(ttl, timedelta):
        raise ExecutionInvariantError("worker_api_ttl_must_be_timedelta")
    seconds = ttl.total_seconds()
    if not seconds.is_integer() or seconds <= 0:
        raise ExecutionInvariantError("worker_api_lease_ttl_must_be_positive_whole_seconds")
    return int(seconds)


def _isoformat(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ExecutionInvariantError("worker_api_datetime_must_be_timezone_aware")
    return value.isoformat()


def _require_nonempty_input_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError(f"worker_api_{field_name}_is_invalid")


def _require_uuid_input(value: str, field_name: str) -> None:
    _require_nonempty_input_text(value, field_name)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExecutionInvariantError(f"worker_api_{field_name}_is_invalid") from exc
    if str(parsed) != value.lower():
        raise ExecutionInvariantError(f"worker_api_{field_name}_is_invalid")


def _require_sha256_input(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ExecutionInvariantError(f"worker_api_{field_name}_is_invalid")


def _validate_cash_settlement_actor(
    *,
    holder_id: str,
    release_sha: str,
    expected_release_sha: str,
    fencing_token: int,
) -> None:
    _require_uuid_input(holder_id, "cash_settlement_holder_id")
    if release_sha != expected_release_sha:
        raise ExecutionInvariantError("worker_api_release_sha_mismatch")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
        raise ExecutionInvariantError("worker_api_fencing_token_is_invalid")
    if fencing_token <= 0:
        raise ExecutionInvariantError("worker_api_fencing_token_is_invalid")


def _validate_unknown_resolution_actor(
    *,
    account_id: str,
    environment: ExecutionEnvironment,
    holder_id: str,
    release_sha: str,
    expected_release_sha: str,
    fencing_token: int,
) -> None:
    expected_account_id = {
        "paper": "paper-primary",
        "contract_test": "contract-test-primary",
    }.get(environment)
    if expected_account_id != account_id:
        raise ExecutionInvariantError(
            "worker_api_unknown_resolution_account_environment_mismatch"
        )
    _require_uuid_input(holder_id, "unknown_resolution_holder_id")
    if release_sha != expected_release_sha:
        raise ExecutionInvariantError("worker_api_release_sha_mismatch")
    if (
        isinstance(fencing_token, bool)
        or not isinstance(fencing_token, int)
        or fencing_token <= 0
    ):
        raise ExecutionInvariantError("worker_api_fencing_token_is_invalid")


def _singleton_row(value: object) -> JsonObject:
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], dict):
            raise ExecutionInvariantError("worker_api_rpc_expected_singleton_row")
        value = value[0]
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ExecutionInvariantError("worker_api_rpc_expected_object_row")
    return value


def _row_set(value: object) -> tuple[JsonObject, ...]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ExecutionInvariantError("worker_api_rpc_expected_row_set")
    if not all(all(isinstance(key, str) for key in row) for row in value):
        raise ExecutionInvariantError("worker_api_rpc_row_has_invalid_key")
    return tuple(value)


def _worker_lease_from_row(row: Mapping[str, object]) -> WorkerLease:
    return WorkerLease(
        account_id=_required_text(row, "account_id"),
        holder_id=_required_text(row, "holder_id"),
        fencing_token=_required_int(row, "fencing_token"),
        acquired_at=_required_datetime(row, "acquired_at"),
        expires_at=_required_datetime(row, "expires_at"),
    )


def _require_exact_keys(row: Mapping[str, object], expected: set[str]) -> None:
    if set(row) != expected:
        raise ExecutionInvariantError("worker_api_rpc_response_fields_are_invalid")


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _required_uuid_text(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid") from exc
    if str(parsed) != value.lower():
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _optional_uuid_text(row: Mapping[str, object], key: str) -> str | None:
    if row.get(key) is None:
        return None
    return _required_uuid_text(row, key)


def _required_sha256_text(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    _require_sha256_input(value, key)
    return value


def _required_release_sha_text(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    if _SHA_RE.fullmatch(value) is None:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _optional_sha256_text(row: Mapping[str, object], key: str) -> str | None:
    value = _optional_text(row, key)
    if value is not None:
        _require_sha256_input(value, key)
    return value


def _required_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _required_positive_int(row: Mapping[str, object], key: str) -> int:
    value = _required_int(row, key)
    if value <= 0:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _required_nonnegative_int(row: Mapping[str, object], key: str) -> int:
    value = _required_int(row, key)
    if value < 0:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _optional_positive_int(row: Mapping[str, object], key: str) -> int | None:
    value = _optional_int(row, key)
    if value is not None and value <= 0:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _optional_nonnegative_int(row: Mapping[str, object], key: str) -> int | None:
    value = _optional_int(row, key)
    if value is not None and value < 0:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _required_bool(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return value


def _required_datetime(row: Mapping[str, object], key: str) -> datetime:
    value = _required_text(row, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return parsed


def _optional_datetime(row: Mapping[str, object], key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    return _required_datetime(row, key)


def _required_date(row: Mapping[str, object], key: str) -> date:
    value = _required_text(row, key)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid") from exc
    if parsed.isoformat() != value:
        raise ExecutionInvariantError("worker_api_rpc_response_field_is_invalid")
    return parsed


def _postgrest_error_message(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    return message if isinstance(message, str) else None

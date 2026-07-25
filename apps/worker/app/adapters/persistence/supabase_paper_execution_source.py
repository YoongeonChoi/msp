from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

import httpx

from app.application.ports.paper_execution_command_source_port import (
    ClaimedPaperExecutionCommand,
    PaperExecutionCommandBundle,
    PaperExecutionSourceCompletion,
    PaperExecutionSourceOutcome,
)
from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    persistence_authority_fingerprint,
)
from app.application.services.scheduler_invocation_deadline import (
    require_scheduler_invocation_effect_authorization,
)
from app.application.use_cases.run_execution_v2 import PaperExecutionV2Command
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionInvariantError,
    MinuteBar,
    PaperExecutionEvidence,
    PaperPositionCostBasis,
)
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.entities import AccountState, BotSettings, Quote, Signal
from app.infrastructure.release_metadata import worker_release_metadata
from app.infrastructure.supabase_headers import supabase_api_headers

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )

PaperSourceRpc = Literal[
    "claim_paper_execution_v1",
    "load_claimed_paper_execution_bundle_v1",
    "complete_paper_execution_source_v1",
]

PAPER_SOURCE_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "claim_paper_execution_v1",
        "load_claimed_paper_execution_bundle_v1",
        "complete_paper_execution_source_v1",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_EXECUTION_SCHEDULER_JOB_KEY: Literal["operations.execution"] = (
    "operations.execution"
)


class SupabasePaperExecutionCommandSource:
    """Strict RPC-only source for durable Paper execution commands.

    The adapter has no table CRUD method and no provider transport.  Its
    account is fixed at construction and the database still requires the
    current account lease, fencing token, control epoch, and qualification.
    """

    __slots__ = (
        "_account_id",
        "_release_sha",
        "_persistence_authority",
        "_base_url",
        "_headers",
        "_client",
        "_managed_client",
    )
    _SEALED_RUNTIME_FIELDS = frozenset(
        {
            "_account_id",
            "_release_sha",
            "_persistence_authority",
            "_base_url",
            "_headers",
            "_client",
            "_managed_client",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._SEALED_RUNTIME_FIELDS and hasattr(self, name):
            raise AttributeError("paper_source_runtime_identity_is_read_only")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self._SEALED_RUNTIME_FIELDS:
            raise AttributeError("paper_source_runtime_identity_is_read_only")
        object.__delattr__(self, name)

    def __init__(
        self,
        settings: Settings,
        *,
        account_id: str,
        release_sha: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.execution_v2_enabled or not settings.execution_v2_worker_api_enabled:
            raise ExecutionInvariantError("paper_source_worker_api_is_not_enabled")
        if settings.execution_v2_environment != "paper":
            raise ExecutionInvariantError("paper_source_requires_paper_environment")
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise ExecutionInvariantError("paper_source_credentials_are_missing")
        if _ACCOUNT_RE.fullmatch(account_id) is None:
            raise ExecutionInvariantError("paper_source_account_id_is_invalid")
        if account_id != settings.execution_v2_account_id:
            raise ExecutionInvariantError("paper_source_configured_account_mismatch")
        resolved_release_sha = release_sha or worker_release_metadata().get("release_sha")
        if (
            not isinstance(resolved_release_sha, str)
            or _SHA_RE.fullmatch(resolved_release_sha) is None
        ):
            raise ExecutionInvariantError("paper_source_release_sha_is_missing")
        try:
            authority = persistence_authority_fingerprint(
                namespace="supabase-worker-api",
                origin=settings.supabase_url,
                profile="worker_api",
            )
        except ValueError:
            raise ExecutionInvariantError("paper_source_origin_is_invalid") from None
        secret = settings.supabase_secret_key.get_secret_value()
        self._account_id = account_id
        self._release_sha = resolved_release_sha.lower()
        self._persistence_authority: PersistenceAuthority = authority
        self._base_url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc"
        self._headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "content-profile": "worker_api",
            "content-type": "application/json",
        }
        if client is None:
            managed_client = httpx.AsyncClient(
                timeout=10.0,
                headers=self._headers,
                trust_env=False,
            )
            self._client = managed_client
            self._managed_client: httpx.AsyncClient | None = managed_client
        else:
            self._client = client
            self._managed_client = None

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def release_sha(self) -> str:
        return self._release_sha

    @property
    def persistence_authority(self) -> PersistenceAuthority:
        return self._persistence_authority

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def transport_is_managed(self) -> bool:
        return self._managed_client is not None and self._client is self._managed_client

    async def close(self) -> None:
        managed_client = self._managed_client
        if managed_client is not None:
            await managed_client.aclose()

    async def claim_available_paper_execution(
        self,
        *,
        worker_id: str,
        release_sha: str,
        now: datetime,
        lease_ttl: timedelta,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ClaimedPaperExecutionCommand | None:
        _require_uuid_input(worker_id, "worker_id")
        self._require_current_release(release_sha)
        ttl_seconds = _positive_ttl_seconds(lease_ttl)
        value = await self._rpc(
            "claim_paper_execution_v1",
            {
                "p_account_id": self.account_id,
                "p_worker_id": worker_id,
                "p_release_sha": self.release_sha,
                "p_now": _isoformat(now),
                "p_lease_seconds": ttl_seconds,
            },
            scheduler_authorization=scheduler_authorization,
            expected_job_key=(
                _EXECUTION_SCHEDULER_JOB_KEY
                if scheduler_authorization is not None
                else None
            ),
        )
        row = _optional_singleton_row(value)
        if row is None:
            return None
        _require_exact_keys(
            row,
            {
                "command_id",
                "intent_id",
                "kind",
                "claim_token",
                "source_revision",
                "worker_id",
                "release_sha",
                "available_at",
                "claimed_at",
                "claim_expires_at",
            },
        )
        claim = ClaimedPaperExecutionCommand(
            command_id=_required_uuid_text(row, "command_id"),
            intent_id=_required_uuid_text(row, "intent_id"),
            kind=cast(Literal["new_candidate", "resume_existing"], _required_text(row, "kind")),
            claim_token=_required_uuid_text(row, "claim_token"),
            source_revision=_required_positive_int(row, "source_revision"),
            worker_id=_required_uuid_text(row, "worker_id"),
            release_sha=_required_release_sha(row, "release_sha"),
            available_at=_required_datetime(row, "available_at"),
            claimed_at=_required_datetime(row, "claimed_at"),
            claim_expires_at=_required_datetime(row, "claim_expires_at"),
        )
        if claim.worker_id != worker_id or claim.release_sha != self.release_sha:
            raise ExecutionInvariantError("paper_source_claim_identity_mismatch")
        return claim

    async def load_claimed_paper_execution_bundle(
        self,
        claim: ClaimedPaperExecutionCommand,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionCommandBundle:
        self._require_claim_binding(claim)
        row = _singleton_row(
            await self._rpc(
                "load_claimed_paper_execution_bundle_v1",
                {
                    "p_command_id": claim.command_id,
                    "p_intent_id": claim.intent_id,
                    "p_claim_token": claim.claim_token,
                    "p_expected_revision": claim.source_revision,
                    "p_worker_id": claim.worker_id,
                    "p_release_sha": self.release_sha,
                    "p_now": _isoformat(now),
                },
                scheduler_authorization=scheduler_authorization,
                expected_job_key=(
                    _EXECUTION_SCHEDULER_JOB_KEY
                    if scheduler_authorization is not None
                    else None
                ),
            )
        )
        _require_exact_keys(row, {"bundle"})
        bundle_value = row.get("bundle")
        if not isinstance(bundle_value, dict):
            raise ExecutionInvariantError("paper_source_bundle_is_invalid")
        return _parse_bundle(
            bundle_value,
            claim=claim,
            expected_account_id=self.account_id,
        )

    async def complete_or_reschedule_paper_execution(
        self,
        *,
        command_id: str,
        claim_token: str,
        expected_revision: int,
        worker_id: str,
        release_sha: str,
        now: datetime,
        outcome: PaperExecutionSourceOutcome,
        next_available_at: datetime | None,
        reason_code: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionSourceCompletion:
        _require_uuid_input(command_id, "command_id")
        _require_uuid_input(claim_token, "claim_token")
        _require_uuid_input(worker_id, "worker_id")
        self._require_current_release(release_sha)
        if isinstance(expected_revision, bool) or expected_revision <= 0:
            raise ExecutionInvariantError("paper_source_revision_is_invalid")
        if outcome not in {"complete", "reschedule", "manual"}:
            raise ExecutionInvariantError("paper_source_outcome_is_invalid")
        if (outcome == "reschedule") != (next_available_at is not None):
            raise ExecutionInvariantError("paper_source_schedule_is_invalid")
        if (
            not isinstance(reason_code, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_]{2,119}", reason_code) is None
        ):
            raise ExecutionInvariantError("paper_source_reason_code_is_invalid")
        row = _singleton_row(
            await self._rpc(
                "complete_paper_execution_source_v1",
                {
                    "p_command_id": command_id,
                    "p_claim_token": claim_token,
                    "p_expected_revision": expected_revision,
                    "p_worker_id": worker_id,
                    "p_release_sha": self.release_sha,
                    "p_now": _isoformat(now),
                    "p_outcome": outcome,
                    "p_next_available_at": (
                        _isoformat(next_available_at) if next_available_at is not None else None
                    ),
                    "p_reason_code": reason_code,
                },
                scheduler_authorization=scheduler_authorization,
                expected_job_key=(
                    _EXECUTION_SCHEDULER_JOB_KEY
                    if scheduler_authorization is not None
                    else None
                ),
            )
        )
        _require_exact_keys(
            row,
            {"command_id", "state", "source_revision", "next_available_at"},
        )
        completion = PaperExecutionSourceCompletion(
            command_id=_required_uuid_text(row, "command_id"),
            state=cast(
                Literal["complete", "pending", "manual"],
                _required_text(row, "state"),
            ),
            source_revision=_required_positive_int(row, "source_revision"),
            next_available_at=_optional_datetime(row, "next_available_at"),
        )
        if completion.command_id != command_id:
            raise ExecutionInvariantError("paper_source_completion_identity_mismatch")
        expected_state = {
            "complete": "complete",
            "reschedule": "pending",
            "manual": "manual",
        }[outcome]
        if completion.state != expected_state:
            raise ExecutionInvariantError("paper_source_completion_state_mismatch")
        if completion.source_revision != expected_revision + 1:
            raise ExecutionInvariantError("paper_source_completion_revision_mismatch")
        if outcome == "reschedule":
            if completion.next_available_at != next_available_at:
                raise ExecutionInvariantError("paper_source_completion_schedule_mismatch")
        elif completion.next_available_at is not None:
            raise ExecutionInvariantError("paper_source_completion_schedule_is_unexpected")
        return completion

    def _require_current_release(self, release_sha: str) -> None:
        if release_sha != self.release_sha:
            raise ExecutionInvariantError("paper_source_release_sha_mismatch")

    def _require_claim_binding(self, claim: ClaimedPaperExecutionCommand) -> None:
        if claim.release_sha != self.release_sha:
            raise ExecutionInvariantError("paper_source_claim_release_mismatch")

    async def _rpc(
        self,
        rpc: PaperSourceRpc,
        payload: JsonObject,
        *,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None,
        expected_job_key: Literal["operations.execution"] | None,
    ) -> object:
        if rpc not in PAPER_SOURCE_RPC_ALLOWLIST:
            raise ExecutionInvariantError("paper_source_rpc_is_not_allowed")
        if (scheduler_authorization is None) != (expected_job_key is None):
            raise ExecutionInvariantError(
                "paper_source_scheduler_authorization_pair_is_invalid"
            )
        if expected_job_key not in {None, _EXECUTION_SCHEDULER_JOB_KEY}:
            raise ExecutionInvariantError("paper_source_scheduler_job_key_is_invalid")
        try:
            if scheduler_authorization is not None:
                require_scheduler_invocation_effect_authorization(
                    scheduler_authorization,
                    expected_job_key=_EXECUTION_SCHEDULER_JOB_KEY,
                )
            response = await self._client.post(
                f"{self._base_url}/{rpc}",
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionInvariantError(
                "paper_source_rpc_failed_or_returned_invalid_json"
            ) from exc


def _parse_bundle(
    value: Mapping[str, object],
    *,
    claim: ClaimedPaperExecutionCommand,
    expected_account_id: str,
) -> PaperExecutionCommandBundle:
    _require_exact_keys(value, {"schema_version", "command", "risk_input"})
    if _required_int(value, "schema_version") != 1:
        raise ExecutionInvariantError("paper_source_bundle_schema_version_is_invalid")
    command_row = _required_object(value, "command")
    _require_exact_keys(
        command_row,
        {
            "intent",
            "bars",
            "cost_schedule",
            "execution_evidence",
            "position_cost_basis",
            "dispatch_at",
            "evaluated_at",
        },
    )
    intent = _parse_intent(_required_object(command_row, "intent"))
    if intent.id != claim.intent_id:
        raise ExecutionInvariantError("paper_source_bundle_intent_mismatch")
    if intent.account_id != expected_account_id:
        raise ExecutionInvariantError("paper_source_bundle_account_mismatch")
    bars_value = command_row.get("bars")
    if not isinstance(bars_value, list):
        raise ExecutionInvariantError("paper_source_bars_are_invalid")
    bars = tuple(_parse_bar(_object_value(item)) for item in bars_value)
    cost_schedule = _parse_cost_schedule(_required_object(command_row, "cost_schedule"))
    execution_evidence = _parse_execution_evidence(
        _required_object(command_row, "execution_evidence")
    )
    position_value = command_row.get("position_cost_basis")
    position_cost_basis = (
        None
        if position_value is None
        else _parse_position_cost_basis(_object_value(position_value))
    )
    command = PaperExecutionV2Command.create(
        intent=intent,
        bars=bars,
        cost_schedule=cost_schedule,
        execution_evidence=execution_evidence,
        position_cost_basis=position_cost_basis,
        dispatch_at=_required_datetime(command_row, "dispatch_at"),
        evaluated_at=_required_datetime(command_row, "evaluated_at"),
    )
    risk_value = value.get("risk_input")
    risk_input = (
        None if risk_value is None else _parse_risk_input(_object_value(risk_value), intent=intent)
    )
    if claim.kind == "new_candidate" and risk_input is None:
        raise ExecutionInvariantError("paper_source_new_candidate_risk_input_missing")
    if claim.kind == "resume_existing" and risk_input is not None:
        raise ExecutionInvariantError("paper_source_resume_risk_input_is_unexpected")
    return PaperExecutionCommandBundle(command=command, risk_input=risk_input)


def _parse_intent(row: Mapping[str, object]) -> ExecutionIntent:
    _require_exact_keys(
        row,
        {
            "id",
            "decision_id",
            "risk_result_id",
            "decision_feature_sha256",
            "risk_allowed",
            "risk_reason_codes",
            "risk_evaluated_at",
            "risk_expires_at",
            "semantic_key",
            "account_id",
            "environment",
            "strategy_version_id",
            "symbol",
            "side",
            "quantity",
            "limit_price_krw",
            "decision_at",
            "signal_valid_from",
            "signal_valid_until",
            "execution_policy_version",
            "cost_schedule_version",
            "cost_schedule_evidence_sha256",
            "cash_commitment_krw",
            "eligible_at",
            "expires_at",
            "gate_epoch",
            "lease_holder_id",
            "lease_fencing_token",
            "time_in_force",
        },
    )
    reasons_value = row.get("risk_reason_codes")
    if (
        not isinstance(reasons_value, list)
        or not all(isinstance(item, str) and item.strip() for item in reasons_value)
    ) and reasons_value != []:
        raise ExecutionInvariantError("paper_source_risk_reason_codes_are_invalid")
    environment = _required_text(row, "environment")
    side = _required_text(row, "side")
    if environment != "paper" or side not in {"buy", "sell"}:
        raise ExecutionInvariantError("paper_source_intent_environment_or_side_invalid")
    return ExecutionIntent(
        id=_required_uuid_text(row, "id"),
        decision_id=_required_uuid_text(row, "decision_id"),
        risk_result_id=_required_uuid_text(row, "risk_result_id"),
        decision_feature_sha256=_required_sha256(row, "decision_feature_sha256"),
        risk_allowed=_required_bool(row, "risk_allowed"),
        risk_reason_codes=tuple(cast(list[str], reasons_value)),
        risk_evaluated_at=_required_datetime(row, "risk_evaluated_at"),
        risk_expires_at=_required_datetime(row, "risk_expires_at"),
        semantic_key=_required_sha256(row, "semantic_key"),
        account_id=_required_text(row, "account_id"),
        environment="paper",
        strategy_version_id=_required_uuid_text(row, "strategy_version_id"),
        symbol=_required_text(row, "symbol"),
        side=cast(Literal["buy", "sell"], side),
        quantity=_required_positive_int(row, "quantity"),
        limit_price_krw=_required_positive_int(row, "limit_price_krw"),
        decision_at=_required_datetime(row, "decision_at"),
        signal_valid_from=_required_datetime(row, "signal_valid_from"),
        signal_valid_until=_required_datetime(row, "signal_valid_until"),
        execution_policy_version=_required_text(row, "execution_policy_version"),
        cost_schedule_version=_required_text(row, "cost_schedule_version"),
        cost_schedule_evidence_sha256=_required_sha256(row, "cost_schedule_evidence_sha256"),
        cash_commitment_krw=_required_nonnegative_int(row, "cash_commitment_krw"),
        eligible_at=_required_datetime(row, "eligible_at"),
        expires_at=_required_datetime(row, "expires_at"),
        gate_epoch=_required_positive_int(row, "gate_epoch"),
        lease_holder_id=_required_uuid_text(row, "lease_holder_id"),
        lease_fencing_token=_required_positive_int(row, "lease_fencing_token"),
        time_in_force=cast(Literal["DAY"], _required_text(row, "time_in_force")),
    )


def _parse_bar(row: Mapping[str, object]) -> MinuteBar:
    _require_exact_keys(
        row,
        {
            "symbol",
            "minute",
            "completed_at",
            "as_of",
            "source_sha256",
            "is_complete",
            "open_krw",
            "high_krw",
            "low_krw",
            "close_krw",
            "volume",
            "other_intent_filled_quantity",
        },
    )
    return MinuteBar(
        symbol=_required_text(row, "symbol"),
        minute=_required_datetime(row, "minute"),
        completed_at=_required_datetime(row, "completed_at"),
        as_of=_required_datetime(row, "as_of"),
        source_sha256=_required_sha256(row, "source_sha256"),
        is_complete=_required_bool(row, "is_complete"),
        open_krw=_required_positive_int(row, "open_krw"),
        high_krw=_required_positive_int(row, "high_krw"),
        low_krw=_required_positive_int(row, "low_krw"),
        close_krw=_required_positive_int(row, "close_krw"),
        volume=_required_nonnegative_int(row, "volume"),
        other_intent_filled_quantity=_required_nonnegative_int(
            row,
            "other_intent_filled_quantity",
        ),
    )


def _parse_cost_schedule(row: Mapping[str, object]) -> ExecutionCostSchedule:
    _require_exact_keys(
        row,
        {
            "version",
            "effective_from",
            "effective_until",
            "evidence_sha256",
            "settlement_days",
            "settlement_evidence_sha256",
            "buy_commission_rate",
            "sell_commission_rate",
            "sell_tax_rate",
        },
    )
    return ExecutionCostSchedule(
        version=_required_text(row, "version"),
        effective_from=_required_datetime(row, "effective_from"),
        effective_until=_required_datetime(row, "effective_until"),
        evidence_sha256=_required_sha256(row, "evidence_sha256"),
        settlement_days=_required_nonnegative_int(row, "settlement_days"),
        settlement_evidence_sha256=_required_sha256(row, "settlement_evidence_sha256"),
        buy_commission_rate=_required_decimal(row, "buy_commission_rate"),
        sell_commission_rate=_required_decimal(row, "sell_commission_rate"),
        sell_tax_rate=_required_decimal(row, "sell_tax_rate"),
    )


def _parse_execution_evidence(row: Mapping[str, object]) -> PaperExecutionEvidence:
    _require_exact_keys(
        row,
        {
            "version",
            "execution_policy_version",
            "effective_from",
            "effective_until",
            "tick_rule_version",
            "tick_size_krw",
            "tick_rule_evidence_sha256",
            "volume_source",
            "volume_unit",
            "volume_evidence_sha256",
            "corporate_action_status",
            "corporate_action_evidence_sha256",
            "market_calendar_version",
            "market_calendar_status",
            "market_calendar_evidence_sha256",
            "open_session_dates",
        },
    )
    dates_value = row.get("open_session_dates")
    if not isinstance(dates_value, list):
        raise ExecutionInvariantError("paper_source_open_session_dates_are_invalid")
    open_dates = tuple(_date_value(item) for item in dates_value)
    volume_unit = _required_text(row, "volume_unit")
    corporate_status = _required_text(row, "corporate_action_status")
    calendar_status = _required_text(row, "market_calendar_status")
    if volume_unit != "shares":
        raise ExecutionInvariantError("paper_source_volume_unit_is_invalid")
    if corporate_status not in {"not_required", "adjusted"}:
        raise ExecutionInvariantError("paper_source_corporate_action_status_is_invalid")
    if calendar_status != "open_sessions_verified":
        raise ExecutionInvariantError("paper_source_calendar_status_is_invalid")
    return PaperExecutionEvidence(
        version=_required_text(row, "version"),
        execution_policy_version=_required_text(row, "execution_policy_version"),
        effective_from=_required_datetime(row, "effective_from"),
        effective_until=_required_datetime(row, "effective_until"),
        tick_rule_version=_required_text(row, "tick_rule_version"),
        tick_size_krw=_required_positive_int(row, "tick_size_krw"),
        tick_rule_evidence_sha256=_required_sha256(row, "tick_rule_evidence_sha256"),
        volume_source=_required_text(row, "volume_source"),
        volume_unit="shares",
        volume_evidence_sha256=_required_sha256(row, "volume_evidence_sha256"),
        corporate_action_status=cast(Literal["not_required", "adjusted"], corporate_status),
        corporate_action_evidence_sha256=_required_sha256(row, "corporate_action_evidence_sha256"),
        market_calendar_version=_required_text(row, "market_calendar_version"),
        market_calendar_status="open_sessions_verified",
        market_calendar_evidence_sha256=_required_sha256(row, "market_calendar_evidence_sha256"),
        open_session_dates=open_dates,
    )


def _parse_position_cost_basis(row: Mapping[str, object]) -> PaperPositionCostBasis:
    _require_exact_keys(row, {"symbol", "quantity", "total_cost_krw", "accounting_method"})
    method = _required_text(row, "accounting_method")
    if method != "moving_weighted_average_v1":
        raise ExecutionInvariantError("paper_source_cost_basis_method_is_invalid")
    return PaperPositionCostBasis(
        symbol=_required_text(row, "symbol"),
        quantity=_required_positive_int(row, "quantity"),
        total_cost_krw=_required_positive_int(row, "total_cost_krw"),
        accounting_method="moving_weighted_average_v1",
    )


def _parse_risk_input(
    row: Mapping[str, object],
    *,
    intent: ExecutionIntent,
) -> RiskInput:
    _require_exact_keys(
        row,
        {
            "account_state",
            "available_position_quantity",
            "cooldown_active",
            "critical_news_risk",
            "duplicate_order",
            "existing_position_pct",
            "liquidity_ok",
            "market_open",
            "provider_health",
            "quote",
            "schema_version",
            "sector_position_pct",
            "settings",
            "shutdown_requested",
            "signal",
            "strategy_approved",
            "strategy_status",
            "strategy_version_id",
            "volatility_ok",
        },
    )
    if _required_int(row, "schema_version") != 1:
        raise ExecutionInvariantError("paper_source_risk_schema_version_is_invalid")
    settings_row = _required_object(row, "settings")
    _require_exact_keys(
        settings_row,
        {
            "deployment_lock",
            "deployment_target_sha",
            "enabled",
            "live_order_allowed",
            "loop_interval_sec",
            "max_daily_loss_pct",
            "max_daily_order_count",
            "max_order_amount_krw",
            "max_position_pct",
            "max_sector_pct",
            "mode",
            "quote_freshness_sec",
        },
    )
    mode = _required_text(settings_row, "mode")
    if mode != "paper" or _required_bool(settings_row, "live_order_allowed"):
        raise ExecutionInvariantError("paper_source_risk_settings_allow_live")
    deployment_target = _optional_text(settings_row, "deployment_target_sha")
    if deployment_target is not None and _SHA_RE.fullmatch(deployment_target) is None:
        raise ExecutionInvariantError("paper_source_deployment_target_is_invalid")
    settings = BotSettings(
        enabled=_required_bool(settings_row, "enabled"),
        mode="paper",
        live_order_allowed=False,
        deployment_lock=_required_bool(settings_row, "deployment_lock"),
        deployment_target_sha=deployment_target,
        max_order_amount_krw=_required_positive_int(settings_row, "max_order_amount_krw"),
        max_daily_loss_pct=_required_float(settings_row, "max_daily_loss_pct"),
        max_daily_order_count=_required_positive_int(settings_row, "max_daily_order_count"),
        max_position_pct=_required_float(settings_row, "max_position_pct"),
        max_sector_pct=_required_float(settings_row, "max_sector_pct"),
        loop_interval_sec=_required_positive_int(settings_row, "loop_interval_sec"),
        quote_freshness_sec=_required_positive_int(settings_row, "quote_freshness_sec"),
    )
    signal_row = _required_object(row, "signal")
    _require_exact_keys(
        signal_row,
        {
            "action",
            "confidence",
            "final_score",
            "order_amount_krw",
            "reason_json",
            "sector",
            "symbol",
        },
    )
    reason_json = signal_row.get("reason_json")
    if not isinstance(reason_json, dict) or not all(isinstance(key, str) for key in reason_json):
        raise ExecutionInvariantError("paper_source_signal_reason_json_is_invalid")
    action = _required_text(signal_row, "action")
    if action not in {"buy", "sell", "hold"}:
        raise ExecutionInvariantError("paper_source_signal_action_is_invalid")
    signal = Signal(
        symbol=_required_text(signal_row, "symbol"),
        action=cast(Literal["hold", "buy", "sell"], action),
        final_score=_required_float(signal_row, "final_score"),
        confidence=_required_float(signal_row, "confidence"),
        order_amount_krw=_required_nonnegative_int(signal_row, "order_amount_krw"),
        sector=_required_text(signal_row, "sector"),
        reason_json=cast(dict[str, object], reason_json),
    )
    account_value = row.get("account_state")
    account_state = (
        None if account_value is None else _parse_account_state(_object_value(account_value))
    )
    quote_value = row.get("quote")
    quote = None if quote_value is None else _parse_quote(_object_value(quote_value))
    provider_health_value = row.get("provider_health")
    if not isinstance(provider_health_value, dict) or not all(
        isinstance(key, str) and isinstance(item, bool)
        for key, item in provider_health_value.items()
    ):
        raise ExecutionInvariantError("paper_source_provider_health_is_invalid")
    strategy_id = _required_uuid_text(row, "strategy_version_id")
    if strategy_id != intent.strategy_version_id:
        raise ExecutionInvariantError("paper_source_risk_strategy_mismatch")
    return RiskInput(
        settings=settings,
        signal=signal,
        account_state=account_state,
        quote=quote,
        now=intent.risk_evaluated_at,
        provider_health=cast(dict[str, bool], provider_health_value),
        market_open=_optional_bool(row, "market_open"),
        existing_position_pct=_optional_float(row, "existing_position_pct"),
        sector_position_pct=_optional_float(row, "sector_position_pct"),
        available_position_quantity=_optional_int(row, "available_position_quantity"),
        critical_news_risk=_optional_bool(row, "critical_news_risk"),
        liquidity_ok=_optional_bool(row, "liquidity_ok"),
        volatility_ok=_optional_bool(row, "volatility_ok"),
        cooldown_active=_required_bool(row, "cooldown_active"),
        duplicate_order=_required_bool(row, "duplicate_order"),
        strategy_version_id=UUID(strategy_id),
        strategy_status=_required_text(row, "strategy_status"),
        strategy_approved=_required_bool(row, "strategy_approved"),
        shutdown_requested=_required_bool(row, "shutdown_requested"),
    )


def _parse_account_state(row: Mapping[str, object]) -> AccountState:
    _require_exact_keys(
        row,
        {
            "cash_krw",
            "daily_loss_pct",
            "daily_order_count",
            "daily_order_count_verified",
            "equity_krw",
            "synced",
            "synced_at",
        },
    )
    return AccountState(
        synced=_required_bool(row, "synced"),
        cash_krw=_required_nonnegative_int(row, "cash_krw"),
        equity_krw=_required_nonnegative_int(row, "equity_krw"),
        daily_loss_pct=_required_float(row, "daily_loss_pct"),
        daily_order_count=_required_nonnegative_int(row, "daily_order_count"),
        synced_at=_required_datetime(row, "synced_at"),
        daily_order_count_verified=_required_bool(row, "daily_order_count_verified"),
    )


def _parse_quote(row: Mapping[str, object]) -> Quote:
    _require_exact_keys(row, {"symbol", "price_krw", "as_of", "source"})
    return Quote(
        symbol=_required_text(row, "symbol"),
        price_krw=_required_positive_int(row, "price_krw"),
        as_of=_required_datetime(row, "as_of"),
        source=_required_text(row, "source"),
    )


def _optional_singleton_row(value: object) -> JsonObject | None:
    if value == []:
        return None
    return _singleton_row(value)


def _singleton_row(value: object) -> JsonObject:
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], dict):
            raise ExecutionInvariantError("paper_source_rpc_expected_singleton_row")
        value = value[0]
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ExecutionInvariantError("paper_source_rpc_expected_object_row")
    return value


def _object_value(value: object) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ExecutionInvariantError("paper_source_object_value_is_invalid")
    return value


def _required_object(row: Mapping[str, object], key: str) -> JsonObject:
    return _object_value(row.get(key))


def _require_exact_keys(row: Mapping[str, object], expected: set[str]) -> None:
    if set(row) != expected:
        raise ExecutionInvariantError("paper_source_response_fields_are_invalid")


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _required_uuid_text(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    _require_uuid_input(value, key)
    return value


def _required_release_sha(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    if _SHA_RE.fullmatch(value) is None:
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _required_sha256(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    if _SHA256_RE.fullmatch(value) is None:
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _required_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _required_positive_int(row: Mapping[str, object], key: str) -> int:
    value = _required_int(row, key)
    if value <= 0:
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _required_nonnegative_int(row: Mapping[str, object], key: str) -> int:
    value = _required_int(row, key)
    if value < 0:
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _required_float(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return float(value)


def _optional_float(row: Mapping[str, object], key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return float(value)


def _required_bool(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _optional_bool(row: Mapping[str, object], key: str) -> bool | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return value


def _required_datetime(row: Mapping[str, object], key: str) -> datetime:
    value = _required_text(row, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionInvariantError("paper_source_response_field_is_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return parsed


def _optional_datetime(row: Mapping[str, object], key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    return _required_datetime(row, key)


def _required_decimal(row: Mapping[str, object], key: str) -> Decimal:
    value = _required_text(row, key)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ExecutionInvariantError("paper_source_response_field_is_invalid") from exc
    if not parsed.is_finite():
        raise ExecutionInvariantError("paper_source_response_field_is_invalid")
    return parsed


def _date_value(value: object) -> date:
    if not isinstance(value, str):
        raise ExecutionInvariantError("paper_source_open_session_date_is_invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ExecutionInvariantError("paper_source_open_session_date_is_invalid") from exc


def _require_uuid_input(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise ExecutionInvariantError(f"paper_source_{field}_is_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExecutionInvariantError(f"paper_source_{field}_is_invalid") from exc
    if str(parsed) != value.lower():
        raise ExecutionInvariantError(f"paper_source_{field}_is_invalid")


def _positive_ttl_seconds(value: timedelta) -> int:
    if not isinstance(value, timedelta):
        raise ExecutionInvariantError("paper_source_lease_ttl_is_invalid")
    seconds = value.total_seconds()
    if not seconds.is_integer() or not 5 <= seconds <= 300:
        raise ExecutionInvariantError("paper_source_lease_ttl_is_invalid")
    return int(seconds)


def _isoformat(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionInvariantError("paper_source_datetime_must_be_timezone_aware")
    return value.isoformat()

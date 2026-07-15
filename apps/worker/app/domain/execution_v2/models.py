from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.domain.common.errors import KnownFailClosedError

ExecutionEnvironment = Literal["paper", "contract_test"]
ExecutionSide = Literal["buy", "sell"]
ExecutionStatus = Literal[
    "open",
    "partial_filled",
    "filled",
    "expired",
    "canceled",
    "rejected",
    "failed_pre_dispatch",
    "unknown_requires_manual_check",
]

SUPPORTED_EXECUTION_ENVIRONMENTS: frozenset[str] = frozenset({"paper", "contract_test"})
TERMINAL_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {"filled", "expired", "canceled", "rejected", "failed_pre_dispatch"}
)
BLOCKING_EXECUTION_STATUSES: frozenset[str] = frozenset({"unknown_requires_manual_check"})
_KOREAN_MARKET_TIMEZONE = ZoneInfo("Asia/Seoul")


class ExecutionInvariantError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("execution_v2", safe_message)


def validate_execution_environment(value: str) -> ExecutionEnvironment:
    if value == "paper":
        return "paper"
    if value == "contract_test":
        return "contract_test"
    raise ExecutionInvariantError("unsupported_or_production_execution_environment")


def next_full_minute(value: datetime) -> datetime:
    _require_aware(value, "decision_at")
    return value.replace(second=0, microsecond=0) + timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class ExecutionGate:
    account_id: str
    environment: ExecutionEnvironment
    enabled: bool
    control_epoch: int
    effective_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.account_id, "account_id")
        validate_execution_environment(self.environment)
        _require_positive_int(self.control_epoch, "control_epoch")
        _require_aware(self.effective_at, "effective_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.effective_at:
            raise ExecutionInvariantError("execution_gate_expiry_must_follow_effective_time")

    def authorizes(self, environment: ExecutionEnvironment, now: datetime) -> bool:
        _require_aware(now, "now")
        return (
            self.enabled
            and self.environment == environment
            and self.effective_at <= now < self.expires_at
        )


@dataclass(frozen=True, slots=True)
class WorkerLease:
    account_id: str
    holder_id: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.account_id, "account_id")
        _require_text(self.holder_id, "holder_id")
        _require_positive_int(self.fencing_token, "fencing_token")
        _require_aware(self.acquired_at, "acquired_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ExecutionInvariantError("worker_lease_expiry_must_follow_acquisition")

    def is_active(self, now: datetime) -> bool:
        _require_aware(now, "now")
        return self.acquired_at <= now < self.expires_at


@dataclass(frozen=True, slots=True)
class WorkerLeaseRelease:
    account_id: str
    holder_id: str
    fencing_token: int
    released_at: datetime
    idempotent: bool

    def __post_init__(self) -> None:
        _require_text(self.account_id, "released_lease_account_id")
        _require_text(self.holder_id, "released_lease_holder_id")
        _require_positive_int(self.fencing_token, "released_lease_fencing_token")
        _require_aware(self.released_at, "released_lease_released_at")
        if not isinstance(self.idempotent, bool):
            raise ExecutionInvariantError("released_lease_idempotent_flag_is_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionCostSchedule:
    version: str
    effective_from: datetime
    effective_until: datetime
    evidence_sha256: str
    settlement_days: int
    settlement_evidence_sha256: str
    buy_commission_rate: Decimal
    sell_commission_rate: Decimal
    sell_tax_rate: Decimal

    def __post_init__(self) -> None:
        _require_text(self.version, "cost_schedule_version")
        _require_aware(self.effective_from, "cost_schedule_effective_from")
        _require_aware(self.effective_until, "cost_schedule_effective_until")
        if self.effective_until <= self.effective_from:
            raise ExecutionInvariantError("cost_schedule_validity_window_is_invalid")
        _require_sha256(self.evidence_sha256, "cost_schedule_evidence_sha256")
        _require_nonnegative_int(self.settlement_days, "settlement_days")
        if self.settlement_days > 10:
            raise ExecutionInvariantError("settlement_days_exceeds_supported_bound")
        _require_sha256(
            self.settlement_evidence_sha256,
            "settlement_evidence_sha256",
        )
        _require_rate(self.buy_commission_rate, "buy_commission_rate")
        _require_rate(self.sell_commission_rate, "sell_commission_rate")
        _require_rate(self.sell_tax_rate, "sell_tax_rate")
        if self.sell_commission_rate + self.sell_tax_rate >= 1:
            raise ExecutionInvariantError("sell_cost_rates_must_leave_positive_proceeds")

    def covers(self, valid_from: datetime, valid_until: datetime) -> bool:
        _require_aware(valid_from, "cost_schedule_check_from")
        _require_aware(valid_until, "cost_schedule_check_until")
        return self.effective_from <= valid_from and valid_until <= self.effective_until


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    id: str
    decision_id: str
    risk_result_id: str
    decision_feature_sha256: str
    risk_allowed: bool
    risk_reason_codes: tuple[str, ...]
    risk_evaluated_at: datetime
    risk_expires_at: datetime
    semantic_key: str
    account_id: str
    environment: ExecutionEnvironment
    strategy_version_id: str
    symbol: str
    side: ExecutionSide
    quantity: int
    limit_price_krw: int
    decision_at: datetime
    signal_valid_from: datetime
    signal_valid_until: datetime
    execution_policy_version: str
    cost_schedule_version: str
    cost_schedule_evidence_sha256: str
    cash_commitment_krw: int
    eligible_at: datetime
    expires_at: datetime
    gate_epoch: int
    lease_holder_id: str
    lease_fencing_token: int
    time_in_force: Literal["DAY"] = "DAY"

    def __post_init__(self) -> None:
        _require_uuid_text(self.id, "execution_intent_id")
        _require_uuid_text(self.decision_id, "decision_id")
        _require_uuid_text(self.risk_result_id, "risk_result_id")
        _require_sha256(self.decision_feature_sha256, "decision_feature_sha256")
        if self.risk_allowed is not True:
            raise ExecutionInvariantError("execution_intent_requires_allowed_risk_result")
        if not isinstance(self.risk_reason_codes, tuple) or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in self.risk_reason_codes
        ):
            raise ExecutionInvariantError("risk_reason_codes_are_invalid")
        if len(set(self.risk_reason_codes)) != len(self.risk_reason_codes):
            raise ExecutionInvariantError("risk_reason_codes_must_be_unique")
        _require_aware(self.decision_at, "decision_at")
        _require_aware(self.risk_evaluated_at, "risk_evaluated_at")
        _require_aware(self.risk_expires_at, "risk_expires_at")
        if self.risk_evaluated_at > self.decision_at:
            raise ExecutionInvariantError("risk_evaluation_follows_execution_decision")
        if self.risk_expires_at <= self.decision_at:
            raise ExecutionInvariantError("risk_result_is_not_fresh_for_execution_decision")
        _require_semantic_key(self.semantic_key)
        _require_text(self.account_id, "account_id")
        validate_execution_environment(self.environment)
        _require_text(self.strategy_version_id, "strategy_version_id")
        _require_text(self.symbol, "symbol")
        if self.side not in {"buy", "sell"}:
            raise ExecutionInvariantError("unsupported_execution_side")
        _require_positive_int(self.quantity, "quantity")
        _require_positive_int(self.limit_price_krw, "limit_price_krw")
        _require_aware(self.signal_valid_from, "signal_valid_from")
        _require_aware(self.signal_valid_until, "signal_valid_until")
        if self.signal_valid_until <= self.signal_valid_from:
            raise ExecutionInvariantError("signal_validity_window_is_invalid")
        if not self.signal_valid_from <= self.decision_at <= self.signal_valid_until:
            raise ExecutionInvariantError("decision_is_outside_signal_validity_window")
        _require_text(self.execution_policy_version, "execution_policy_version")
        _require_text(self.cost_schedule_version, "cost_schedule_version")
        _require_sha256(
            self.cost_schedule_evidence_sha256,
            "cost_schedule_evidence_sha256",
        )
        _require_nonnegative_int(self.cash_commitment_krw, "cash_commitment_krw")
        gross_commitment = self.quantity * self.limit_price_krw
        if self.side == "buy" and self.cash_commitment_krw < gross_commitment:
            raise ExecutionInvariantError("buy_cash_commitment_is_not_conservative")
        if self.side == "sell" and self.cash_commitment_krw != 0:
            raise ExecutionInvariantError("sell_cash_commitment_must_be_zero")
        _require_aware(self.eligible_at, "eligible_at")
        _require_aware(self.expires_at, "expires_at")
        if self.eligible_at != next_full_minute(self.decision_at):
            raise ExecutionInvariantError("execution_must_start_on_next_full_minute")
        if self.expires_at < self.eligible_at:
            raise ExecutionInvariantError("execution_expiry_precedes_eligibility")
        if self.expires_at > self.signal_valid_until:
            raise ExecutionInvariantError("execution_expiry_exceeds_signal_validity")
        _require_positive_int(self.gate_epoch, "gate_epoch")
        _require_text(self.lease_holder_id, "lease_holder_id")
        _require_positive_int(self.lease_fencing_token, "lease_fencing_token")
        if self.time_in_force != "DAY":
            raise ExecutionInvariantError("only_day_time_in_force_is_supported")
        expected_semantic_key = build_semantic_key(
            account_id=self.account_id,
            environment=self.environment,
            strategy_version_id=self.strategy_version_id,
            symbol=self.symbol,
            side=self.side,
            signal_valid_from=self.signal_valid_from,
            signal_valid_until=self.signal_valid_until,
            execution_policy_version=self.execution_policy_version,
        )
        if self.semantic_key != expected_semantic_key:
            raise ExecutionInvariantError("semantic_key_does_not_match_execution_semantics")

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        environment: ExecutionEnvironment,
        decision_id: str,
        risk_result_id: str,
        decision_feature_sha256: str,
        risk_allowed: bool,
        risk_reason_codes: tuple[str, ...],
        risk_evaluated_at: datetime,
        risk_expires_at: datetime,
        strategy_version_id: str,
        symbol: str,
        side: ExecutionSide,
        quantity: int,
        limit_price_krw: int,
        decision_at: datetime,
        signal_valid_from: datetime,
        signal_valid_until: datetime,
        execution_policy_version: str,
        cost_schedule: ExecutionCostSchedule,
        expires_at: datetime,
        gate_epoch: int,
        lease_holder_id: str,
        lease_fencing_token: int,
    ) -> ExecutionIntent:
        environment = validate_execution_environment(environment)
        eligible_at = next_full_minute(decision_at)
        if not cost_schedule.covers(eligible_at, expires_at):
            raise ExecutionInvariantError("intent_cost_schedule_is_not_effective")
        gross_commitment = quantity * limit_price_krw
        cash_commitment_krw = (
            gross_commitment
            + int(
                (Decimal(gross_commitment) * cost_schedule.buy_commission_rate).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            if side == "buy"
            else 0
        )
        semantic_key = build_semantic_key(
            account_id=account_id,
            environment=environment,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            side=side,
            signal_valid_from=signal_valid_from,
            signal_valid_until=signal_valid_until,
            execution_policy_version=execution_policy_version,
        )
        return cls(
            id=str(uuid4()),
            decision_id=decision_id,
            risk_result_id=risk_result_id,
            decision_feature_sha256=decision_feature_sha256,
            risk_allowed=risk_allowed,
            risk_reason_codes=risk_reason_codes,
            risk_evaluated_at=risk_evaluated_at,
            risk_expires_at=risk_expires_at,
            semantic_key=semantic_key,
            account_id=account_id,
            environment=environment,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price_krw=limit_price_krw,
            decision_at=decision_at,
            signal_valid_from=signal_valid_from,
            signal_valid_until=signal_valid_until,
            execution_policy_version=execution_policy_version,
            cost_schedule_version=cost_schedule.version,
            cost_schedule_evidence_sha256=cost_schedule.evidence_sha256,
            cash_commitment_krw=cash_commitment_krw,
            eligible_at=eligible_at,
            expires_at=expires_at,
            gate_epoch=gate_epoch,
            lease_holder_id=lease_holder_id,
            lease_fencing_token=lease_fencing_token,
        )


def build_semantic_key(
    *,
    account_id: str,
    environment: ExecutionEnvironment,
    strategy_version_id: str,
    symbol: str,
    side: ExecutionSide,
    signal_valid_from: datetime,
    signal_valid_until: datetime,
    execution_policy_version: str,
) -> str:
    encoded = canonical_semantic_key_payload(
        account_id=account_id,
        environment=environment,
        strategy_version_id=strategy_version_id,
        symbol=symbol,
        side=side,
        signal_valid_from=signal_valid_from,
        signal_valid_until=signal_valid_until,
        execution_policy_version=execution_policy_version,
    )
    return hashlib.sha256(encoded).hexdigest()


def canonical_semantic_key_payload(
    *,
    account_id: str,
    environment: ExecutionEnvironment,
    strategy_version_id: str,
    symbol: str,
    side: ExecutionSide,
    signal_valid_from: datetime,
    signal_valid_until: datetime,
    execution_policy_version: str,
) -> bytes:
    """Return the exact UTF-8 canonical payload shared with the SQL kernel."""

    _require_text(account_id, "account_id")
    environment = validate_execution_environment(environment)
    _require_text(strategy_version_id, "strategy_version_id")
    _require_text(symbol, "symbol")
    if side not in {"buy", "sell"}:
        raise ExecutionInvariantError("unsupported_execution_side")
    _require_aware(signal_valid_from, "signal_valid_from")
    _require_aware(signal_valid_until, "signal_valid_until")
    if signal_valid_until <= signal_valid_from:
        raise ExecutionInvariantError("signal_validity_window_is_invalid")
    _require_text(execution_policy_version, "execution_policy_version")
    payload = {
        "account_id": account_id,
        "environment": environment,
        "execution_policy_version": execution_policy_version,
        "side": side,
        "signal_valid_from": signal_valid_from.astimezone(UTC).isoformat(),
        "signal_valid_until": signal_valid_until.astimezone(UTC).isoformat(),
        "strategy_version_id": strategy_version_id,
        "symbol": symbol,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return encoded.encode("utf-8")


@dataclass(frozen=True, slots=True)
class MinuteBar:
    symbol: str
    minute: datetime
    completed_at: datetime
    as_of: datetime
    source_sha256: str
    is_complete: bool
    open_krw: int
    high_krw: int
    low_krw: int
    close_krw: int
    volume: int
    other_intent_filled_quantity: int = 0

    def __post_init__(self) -> None:
        _require_text(self.symbol, "bar_symbol")
        _require_aware(self.minute, "bar_minute")
        if self.minute.second != 0 or self.minute.microsecond != 0:
            raise ExecutionInvariantError("bar_timestamp_must_be_full_minute")
        _require_aware(self.completed_at, "bar_completed_at")
        _require_aware(self.as_of, "bar_as_of")
        _require_sha256(self.source_sha256, "bar_source_sha256")
        if not isinstance(self.is_complete, bool):
            raise ExecutionInvariantError("bar_is_complete_must_be_boolean")
        if self.is_complete and self.completed_at < self.minute + timedelta(minutes=1):
            raise ExecutionInvariantError("completed_bar_must_follow_minute_end")
        if self.is_complete and self.as_of < self.completed_at:
            raise ExecutionInvariantError("completed_bar_as_of_precedes_completion")
        _require_positive_int(self.open_krw, "bar_open_krw")
        _require_positive_int(self.high_krw, "bar_high_krw")
        _require_positive_int(self.low_krw, "bar_low_krw")
        _require_positive_int(self.close_krw, "bar_close_krw")
        _require_nonnegative_int(self.volume, "bar_volume")
        _require_nonnegative_int(
            self.other_intent_filled_quantity,
            "bar_other_intent_filled_quantity",
        )
        if self.high_krw < max(self.open_krw, self.close_krw, self.low_krw):
            raise ExecutionInvariantError("bar_high_is_inconsistent")
        if self.low_krw > min(self.open_krw, self.close_krw, self.high_krw):
            raise ExecutionInvariantError("bar_low_is_inconsistent")


@dataclass(frozen=True, slots=True)
class PaperExecutionEvidence:
    version: str
    execution_policy_version: str
    effective_from: datetime
    effective_until: datetime
    tick_rule_version: str
    tick_size_krw: int
    tick_rule_evidence_sha256: str
    volume_source: str
    volume_unit: Literal["shares"]
    volume_evidence_sha256: str
    corporate_action_status: Literal["not_required", "adjusted"]
    corporate_action_evidence_sha256: str
    market_calendar_version: str
    market_calendar_status: Literal["open_sessions_verified"]
    market_calendar_evidence_sha256: str
    open_session_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        _require_text(self.version, "execution_evidence_version")
        _require_text(self.execution_policy_version, "execution_policy_version")
        _require_aware(self.effective_from, "execution_evidence_effective_from")
        _require_aware(self.effective_until, "execution_evidence_effective_until")
        if self.effective_until <= self.effective_from:
            raise ExecutionInvariantError("execution_evidence_validity_window_is_invalid")
        _require_text(self.tick_rule_version, "tick_rule_version")
        _require_positive_int(self.tick_size_krw, "tick_size_krw")
        _require_sha256(self.tick_rule_evidence_sha256, "tick_rule_evidence_sha256")
        _require_text(self.volume_source, "volume_source")
        if self.volume_unit != "shares":
            raise ExecutionInvariantError("volume_unit_must_be_shares")
        _require_sha256(self.volume_evidence_sha256, "volume_evidence_sha256")
        if self.corporate_action_status not in {"not_required", "adjusted"}:
            raise ExecutionInvariantError("corporate_action_status_is_unverified")
        _require_sha256(
            self.corporate_action_evidence_sha256,
            "corporate_action_evidence_sha256",
        )
        _require_text(self.market_calendar_version, "market_calendar_version")
        if self.market_calendar_status != "open_sessions_verified":
            raise ExecutionInvariantError("market_calendar_is_not_verified")
        _require_sha256(
            self.market_calendar_evidence_sha256,
            "market_calendar_evidence_sha256",
        )
        if (
            not self.open_session_dates
            or any(not isinstance(item, date) for item in self.open_session_dates)
            or tuple(sorted(set(self.open_session_dates))) != self.open_session_dates
        ):
            raise ExecutionInvariantError("market_calendar_open_sessions_are_invalid")

    def covers(self, valid_from: datetime, valid_until: datetime) -> bool:
        _require_aware(valid_from, "execution_evidence_check_from")
        _require_aware(valid_until, "execution_evidence_check_until")
        return self.effective_from <= valid_from and valid_until <= self.effective_until

    def validates_price(self, price_krw: int) -> bool:
        return price_krw > 0 and price_krw % self.tick_size_krw == 0

    def settlement_date_for(self, filled_at: datetime, settlement_days: int) -> date:
        _require_aware(filled_at, "settlement_filled_at")
        _require_nonnegative_int(settlement_days, "settlement_days")
        trade_date = filled_at.astimezone(_KOREAN_MARKET_TIMEZONE).date()
        try:
            trade_index = self.open_session_dates.index(trade_date)
            return self.open_session_dates[trade_index + settlement_days]
        except (ValueError, IndexError) as exc:
            raise ExecutionInvariantError(
                "market_calendar_does_not_cover_settlement"
            ) from exc


@dataclass(frozen=True, slots=True)
class PaperPositionCostBasis:
    """FVTPL-style v1: gross fills enter MWA; commissions remain period expenses."""

    symbol: str
    quantity: int
    total_cost_krw: int
    accounting_method: Literal["moving_weighted_average_v1"] = "moving_weighted_average_v1"

    def __post_init__(self) -> None:
        _require_text(self.symbol, "position_cost_symbol")
        _require_positive_int(self.quantity, "position_cost_quantity")
        _require_positive_int(self.total_cost_krw, "position_total_cost_krw")
        if self.total_cost_krw < self.quantity:
            raise ExecutionInvariantError("position_average_cost_must_be_at_least_one_krw")
        if self.accounting_method != "moving_weighted_average_v1":
            raise ExecutionInvariantError("unsupported_position_accounting_method")


@dataclass(frozen=True, slots=True)
class PaperFill:
    sequence: int
    filled_at: datetime
    quantity: int
    price_krw: int
    commission_krw: int
    tax_krw: int
    settlement_date: date
    position_cost_relief_krw: int = 0
    realized_pnl_krw: int = 0

    def __post_init__(self) -> None:
        _require_positive_int(self.sequence, "fill_sequence")
        _require_aware(self.filled_at, "filled_at")
        _require_positive_int(self.quantity, "fill_quantity")
        _require_positive_int(self.price_krw, "fill_price_krw")
        _require_nonnegative_int(self.commission_krw, "fill_commission_krw")
        _require_nonnegative_int(self.tax_krw, "fill_tax_krw")
        if not isinstance(self.settlement_date, date):
            raise ExecutionInvariantError("fill_settlement_date_is_invalid")
        if self.settlement_date < self.filled_at.astimezone(
            _KOREAN_MARKET_TIMEZONE
        ).date():
            raise ExecutionInvariantError("fill_settlement_date_precedes_fill")
        _require_nonnegative_int(
            self.position_cost_relief_krw,
            "position_cost_relief_krw",
        )
        _require_int(self.realized_pnl_krw, "realized_pnl_krw")

    @property
    def gross_amount_krw(self) -> int:
        return self.quantity * self.price_krw


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    intent_id: str
    sequence: int
    status: ExecutionStatus
    observed_at: datetime
    provider_order_id: str
    provider_execution_id: str | None
    provider_observation_sha256: str
    cumulative_quantity: int
    cumulative_gross_krw: int
    cumulative_commission_krw: int
    cumulative_tax_krw: int
    last_fill_quantity: int | None = None
    last_fill_price_krw: int | None = None
    last_fill_settlement_date: date | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.intent_id, "observation_intent_id")
        _require_positive_int(self.sequence, "observation_sequence")
        _require_aware(self.observed_at, "observed_at")
        _require_text(self.provider_order_id, "provider_order_id")
        _require_sha256(
            self.provider_observation_sha256,
            "provider_observation_sha256",
        )
        _require_nonnegative_int(self.cumulative_quantity, "cumulative_quantity")
        _require_nonnegative_int(self.cumulative_gross_krw, "cumulative_gross_krw")
        _require_nonnegative_int(
            self.cumulative_commission_krw, "cumulative_commission_krw"
        )
        _require_nonnegative_int(self.cumulative_tax_krw, "cumulative_tax_krw")
        if (self.last_fill_quantity is None) != (self.last_fill_price_krw is None):
            raise ExecutionInvariantError("observation_fill_fields_must_be_paired")
        if self.last_fill_quantity is not None:
            _require_positive_int(self.last_fill_quantity, "last_fill_quantity")
            _require_positive_int(self.last_fill_price_krw, "last_fill_price_krw")
            _require_text(self.provider_execution_id, "provider_execution_id")
            if not isinstance(self.last_fill_settlement_date, date):
                raise ExecutionInvariantError("observation_settlement_date_is_required")
        elif self.provider_execution_id is not None or self.last_fill_settlement_date is not None:
            raise ExecutionInvariantError("nonfill_observation_has_fill_identity")
        if self.status == "filled" and self.cumulative_quantity == 0:
            raise ExecutionInvariantError("filled_observation_requires_quantity")
        if self.reason is not None:
            _require_text(self.reason, "observation_reason")
        expected_hash = build_provider_observation_sha256(
            intent_id=self.intent_id,
            sequence=self.sequence,
            status=self.status,
            observed_at=self.observed_at,
            provider_order_id=self.provider_order_id,
            provider_execution_id=self.provider_execution_id,
            cumulative_quantity=self.cumulative_quantity,
            cumulative_gross_krw=self.cumulative_gross_krw,
            cumulative_commission_krw=self.cumulative_commission_krw,
            cumulative_tax_krw=self.cumulative_tax_krw,
            last_fill_quantity=self.last_fill_quantity,
            last_fill_price_krw=self.last_fill_price_krw,
            last_fill_settlement_date=self.last_fill_settlement_date,
            reason=self.reason,
        )
        if self.provider_observation_sha256 != expected_hash:
            raise ExecutionInvariantError("provider_observation_sha256_mismatch")

    @classmethod
    def create(
        cls,
        *,
        intent_id: str,
        sequence: int,
        status: ExecutionStatus,
        observed_at: datetime,
        provider_order_id: str,
        provider_execution_id: str | None,
        cumulative_quantity: int,
        cumulative_gross_krw: int,
        cumulative_commission_krw: int,
        cumulative_tax_krw: int,
        last_fill_quantity: int | None = None,
        last_fill_price_krw: int | None = None,
        last_fill_settlement_date: date | None = None,
        reason: str | None = None,
    ) -> ExecutionObservation:
        provider_observation_sha256 = build_provider_observation_sha256(
            intent_id=intent_id,
            sequence=sequence,
            status=status,
            observed_at=observed_at,
            provider_order_id=provider_order_id,
            provider_execution_id=provider_execution_id,
            cumulative_quantity=cumulative_quantity,
            cumulative_gross_krw=cumulative_gross_krw,
            cumulative_commission_krw=cumulative_commission_krw,
            cumulative_tax_krw=cumulative_tax_krw,
            last_fill_quantity=last_fill_quantity,
            last_fill_price_krw=last_fill_price_krw,
            last_fill_settlement_date=last_fill_settlement_date,
            reason=reason,
        )
        return cls(
            intent_id=intent_id,
            sequence=sequence,
            status=status,
            observed_at=observed_at,
            provider_order_id=provider_order_id,
            provider_execution_id=provider_execution_id,
            provider_observation_sha256=provider_observation_sha256,
            cumulative_quantity=cumulative_quantity,
            cumulative_gross_krw=cumulative_gross_krw,
            cumulative_commission_krw=cumulative_commission_krw,
            cumulative_tax_krw=cumulative_tax_krw,
            last_fill_quantity=last_fill_quantity,
            last_fill_price_krw=last_fill_price_krw,
            last_fill_settlement_date=last_fill_settlement_date,
            reason=reason,
        )


def build_provider_observation_sha256(
    *,
    intent_id: str,
    sequence: int,
    status: ExecutionStatus,
    observed_at: datetime,
    provider_order_id: str,
    provider_execution_id: str | None,
    cumulative_quantity: int,
    cumulative_gross_krw: int,
    cumulative_commission_krw: int,
    cumulative_tax_krw: int,
    last_fill_quantity: int | None,
    last_fill_price_krw: int | None,
    last_fill_settlement_date: date | None,
    reason: str | None,
) -> str:
    _require_aware(observed_at, "provider_observation_observed_at")
    fields = (
        intent_id,
        str(sequence),
        status,
        provider_order_id,
        provider_execution_id or "",
        observed_at.astimezone(UTC).isoformat(),
        str(cumulative_quantity),
        str(cumulative_gross_krw),
        str(cumulative_commission_krw),
        str(cumulative_tax_krw),
        "" if last_fill_quantity is None else str(last_fill_quantity),
        "" if last_fill_price_krw is None else str(last_fill_price_krw),
        (
            last_fill_settlement_date.isoformat()
            if last_fill_settlement_date is not None
            else ""
        ),
        reason or "",
    )
    encoded = "|".join(fields)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    account: str
    debit_krw: int = 0
    credit_krw: int = 0

    def __post_init__(self) -> None:
        _require_text(self.account, "ledger_account")
        _require_nonnegative_int(self.debit_krw, "ledger_debit_krw")
        _require_nonnegative_int(self.credit_krw, "ledger_credit_krw")
        if (self.debit_krw > 0) == (self.credit_krw > 0):
            raise ExecutionInvariantError("ledger_posting_requires_exactly_one_side")


@dataclass(frozen=True, slots=True)
class AccountingTransaction:
    id: str
    intent_id: str
    observation_sequence: int
    posted_at: datetime
    postings: tuple[LedgerPosting, ...]

    def __post_init__(self) -> None:
        _require_text(self.id, "accounting_transaction_id")
        _require_text(self.intent_id, "accounting_intent_id")
        _require_positive_int(self.observation_sequence, "accounting_observation_sequence")
        _require_aware(self.posted_at, "accounting_posted_at")
        if not self.postings:
            raise ExecutionInvariantError("accounting_transaction_requires_postings")
        if self.total_debit_krw != self.total_credit_krw:
            raise ExecutionInvariantError("accounting_transaction_is_not_balanced")

    @property
    def total_debit_krw(self) -> int:
        return sum(posting.debit_krw for posting in self.postings)

    @property
    def total_credit_krw(self) -> int:
        return sum(posting.credit_krw for posting in self.postings)


@dataclass(frozen=True, slots=True)
class PaperSimulationResult:
    fills: tuple[PaperFill, ...]
    observations: tuple[ExecutionObservation, ...]
    accounting_transactions: tuple[AccountingTransaction, ...]

    def __post_init__(self) -> None:
        if len(self.fills) != len(self.accounting_transactions):
            raise ExecutionInvariantError("each_paper_fill_requires_accounting_transaction")
        if self.fills and not self.observations:
            raise ExecutionInvariantError("paper_fills_require_observations")


@dataclass(frozen=True, slots=True)
class ObservationRecordResult:
    observation_id: str
    inserted: bool
    quarantined: bool
    reason_code: str

    def __post_init__(self) -> None:
        _require_uuid_text(self.observation_id, "observation_id")
        if not isinstance(self.inserted, bool) or not isinstance(self.quarantined, bool):
            raise ExecutionInvariantError("observation_record_flags_are_invalid")
        if self.inserted and self.quarantined:
            raise ExecutionInvariantError("inserted_observation_cannot_be_quarantined")
        _require_text(self.reason_code, "observation_record_reason_code")


@dataclass(frozen=True, slots=True)
class OrderIntentReservationResult:
    state: Literal["created", "existing_replay", "semantic_duplicate"]
    intent_id: str
    reservation_id: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.state not in {"created", "existing_replay", "semantic_duplicate"}:
            raise ExecutionInvariantError("reservation_result_state_is_invalid")
        _require_uuid_text(self.intent_id, "reservation_result_intent_id")
        _require_uuid_text(self.reservation_id, "reservation_result_reservation_id")
        _require_text(self.reason_code, "reservation_result_reason_code")


@dataclass(frozen=True, slots=True)
class PaperExecutionCheckpoint:
    """Fenced durable prefix used to resume deterministic paper execution."""

    intent_id: str
    attempt_id: str
    provider_order_id: str | None
    latest_sequence: int | None
    latest_status: ExecutionStatus | None
    latest_observed_at: datetime | None
    cumulative_quantity: int
    cumulative_gross_krw: int
    cumulative_commission_krw: int
    cumulative_tax_krw: int
    observation_history_sha256: str | None
    expires_at: datetime
    intent_release_sha: str
    lease_release_sha: str
    position_cost_basis_method: Literal["moving_weighted_average_v1"] | None
    position_quantity_snapshot: int | None
    position_total_cost_krw: int | None
    position_cost_basis_sha256: str | None

    def __post_init__(self) -> None:
        _require_uuid_text(self.intent_id, "paper_checkpoint_intent_id")
        _require_uuid_text(self.attempt_id, "paper_checkpoint_attempt_id")
        if self.provider_order_id is not None:
            _require_text(self.provider_order_id, "paper_checkpoint_provider_order_id")
        latest = (
            self.latest_sequence,
            self.latest_status,
            self.latest_observed_at,
            self.observation_history_sha256,
        )
        if any(value is None for value in latest) and any(
            value is not None for value in latest
        ):
            raise ExecutionInvariantError("paper_checkpoint_latest_observation_is_partial")
        for value, field in (
            (self.cumulative_quantity, "paper_checkpoint_cumulative_quantity"),
            (self.cumulative_gross_krw, "paper_checkpoint_cumulative_gross_krw"),
            (self.cumulative_commission_krw, "paper_checkpoint_cumulative_commission_krw"),
            (self.cumulative_tax_krw, "paper_checkpoint_cumulative_tax_krw"),
        ):
            _require_nonnegative_int(value, field)
        if self.latest_sequence is None:
            if self.provider_order_id is not None or any(
                value != 0
                for value in (
                    self.cumulative_quantity,
                    self.cumulative_gross_krw,
                    self.cumulative_commission_krw,
                    self.cumulative_tax_krw,
                )
            ):
                raise ExecutionInvariantError("paper_checkpoint_empty_prefix_is_invalid")
        else:
            _require_positive_int(self.latest_sequence, "paper_checkpoint_latest_sequence")
            assert self.latest_observed_at is not None
            assert self.observation_history_sha256 is not None
            _require_aware(self.latest_observed_at, "paper_checkpoint_latest_observed_at")
            _require_sha256(
                self.observation_history_sha256,
                "paper_checkpoint_observation_history_sha256",
            )
            if self.provider_order_id != f"paper:{self.intent_id}":
                raise ExecutionInvariantError(
                    "paper_checkpoint_provider_order_identity_is_invalid"
                )
        _require_aware(self.expires_at, "paper_checkpoint_expires_at")
        _require_release_sha(self.intent_release_sha, "paper_checkpoint_intent_release_sha")
        _require_release_sha(self.lease_release_sha, "paper_checkpoint_lease_release_sha")
        position_cost_basis = (
            self.position_cost_basis_method,
            self.position_quantity_snapshot,
            self.position_total_cost_krw,
            self.position_cost_basis_sha256,
        )
        if any(value is None for value in position_cost_basis) and any(
            value is not None for value in position_cost_basis
        ):
            raise ExecutionInvariantError("paper_checkpoint_position_cost_basis_is_partial")
        if self.position_cost_basis_method is not None:
            if self.position_cost_basis_method != "moving_weighted_average_v1":
                raise ExecutionInvariantError(
                    "paper_checkpoint_position_cost_basis_method_is_invalid"
                )
            _require_positive_int(
                self.position_quantity_snapshot,
                "paper_checkpoint_position_quantity_snapshot",
            )
            _require_positive_int(
                self.position_total_cost_krw,
                "paper_checkpoint_position_total_cost_krw",
            )
            assert self.position_cost_basis_sha256 is not None
            _require_sha256(
                self.position_cost_basis_sha256,
                "paper_checkpoint_position_cost_basis_sha256",
            )

    @property
    def is_terminal(self) -> bool:
        return self.latest_status in TERMINAL_EXECUTION_STATUSES

    def pinned_position_cost_basis(self, symbol: str) -> PaperPositionCostBasis | None:
        if self.position_cost_basis_method is None:
            return None
        assert self.position_quantity_snapshot is not None
        assert self.position_total_cost_krw is not None
        return PaperPositionCostBasis(
            symbol=symbol,
            quantity=self.position_quantity_snapshot,
            total_cost_krw=self.position_total_cost_krw,
        )


def build_observation_history_sha256(
    observations: tuple[ExecutionObservation, ...],
) -> str:
    if not observations:
        raise ExecutionInvariantError("paper_observation_history_must_be_nonempty")
    material = "|".join(
        f"{observation.sequence}:{observation.provider_observation_sha256}"
        for observation in observations
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

@dataclass(frozen=True, slots=True)
class PaperPositionSnapshot:
    symbol: str
    quantity: int
    total_cost_krw: int

    def __post_init__(self) -> None:
        _require_text(self.symbol, "position_symbol")
        _require_positive_int(self.quantity, "position_quantity")
        _require_positive_int(self.total_cost_krw, "position_total_cost_krw")
        if self.total_cost_krw < self.quantity:
            raise ExecutionInvariantError("position_average_cost_must_be_at_least_one_krw")

    @property
    def average_cost_krw(self) -> Decimal:
        return Decimal(self.total_cost_krw) / Decimal(self.quantity)


@dataclass(frozen=True, slots=True)
class PaperAccountSnapshot:
    account_id: str
    cash_krw: int
    reserved_cash_krw: int
    positions: tuple[PaperPositionSnapshot, ...]
    reserved_position_quantities: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _require_text(self.account_id, "account_id")
        _require_nonnegative_int(self.cash_krw, "cash_krw")
        _require_nonnegative_int(self.reserved_cash_krw, "reserved_cash_krw")
        for symbol, quantity in self.reserved_position_quantities:
            _require_text(symbol, "reserved_position_symbol")
            _require_nonnegative_int(quantity, "reserved_position_quantity")

    def quantity_for(self, symbol: str) -> int:
        return next(
            (position.quantity for position in self.positions if position.symbol == symbol),
            0,
        )

    def average_cost_for(self, symbol: str) -> Decimal | None:
        return next(
            (
                position.average_cost_krw
                for position in self.positions
                if position.symbol == symbol
            ),
            None,
        )

    def reserved_quantity_for(self, symbol: str) -> int:
        return dict(self.reserved_position_quantities).get(symbol, 0)


def new_accounting_transaction_id(intent_id: str, sequence: int) -> str:
    value = f"{intent_id}:fill:{sequence}".encode()
    return hashlib.sha256(value).hexdigest()


def _require_text(value: str | None, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError(f"{field_name}_must_be_nonempty")


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionInvariantError(f"{field_name}_must_be_timezone_aware")


def _require_positive_int(value: int | None, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionInvariantError(f"{field_name}_must_be_positive_integer")


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionInvariantError(f"{field_name}_must_be_nonnegative_integer")


def _require_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionInvariantError(f"{field_name}_must_be_integer")


def _require_rate(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value >= 1:
        raise ExecutionInvariantError(f"{field_name}_must_be_decimal_between_zero_and_one")


def _require_semantic_key(value: str) -> None:
    _require_sha256(value, "semantic_key")


def _require_uuid_text(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExecutionInvariantError(f"{field_name}_must_be_uuid") from exc
    if str(parsed) != value.lower():
        raise ExecutionInvariantError(f"{field_name}_must_be_canonical_uuid")


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ExecutionInvariantError(f"{field_name}_must_be_sha256_hex")


def _require_release_sha(value: str, field_name: str) -> None:
    if len(value) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ExecutionInvariantError(f"{field_name}_must_be_release_sha")

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.adapters.broker.contract_test_broker import (
    QUALIFIED_TOSS_OPENAPI_SHA256,
    ContractCancelOutcome,
    ContractPlacementOutcome,
    ContractStatusOutcome,
    ContractTestBroker,
)
from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import BrokerOrderRequest
from app.application.services.execution_service import ExecutionService
from app.application.services.risk_service import RiskService
from app.domain.common.errors import (
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionGate,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    PaperExecutionEvidence,
)
from app.domain.trading.entities import BotSettings

ContractQualificationCheckId = Literal[
    "cancel_lifecycle",
    "create_lifecycle",
    "fault_injection",
    "ledger_invariants",
    "production_order_network_zero",
    "status_partial_terminal",
]
ContractQualificationStatus = Literal["pass", "fail"]
ContractMetricValue = bool | int | str

_CHECK_IDS: tuple[ContractQualificationCheckId, ...] = (
    "cancel_lifecycle",
    "create_lifecycle",
    "fault_injection",
    "ledger_invariants",
    "production_order_network_zero",
    "status_partial_terminal",
)


@dataclass(frozen=True, slots=True)
class ContractQualificationCheck:
    check_id: ContractQualificationCheckId
    status: ContractQualificationStatus
    evidence_sha256: str
    metrics: Mapping[str, ContractMetricValue]

    def to_manifest_item(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "evidence_sha256": self.evidence_sha256,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class ContractQualificationReport:
    suite_version: str
    openapi_sha256: str
    started_at: datetime
    completed_at: datetime
    result: ContractQualificationStatus
    checks: tuple[ContractQualificationCheck, ...]

    def evidence_manifest(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "checks": [check.to_manifest_item() for check in self.checks],
        }

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "suite_version": self.suite_version,
            "environment": "contract_test",
            "execution_transport": "local_contract_simulator",
            "openapi_sha256": self.openapi_sha256,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "result": self.result,
            "evidence_manifest": self.evidence_manifest(),
        }


class _QualificationCheckFailed(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


CheckRunner = Callable[[], Awaitable[Mapping[str, ContractMetricValue]]]
Clock = Callable[[], datetime]


class RunContractQualification:
    """Exercise the pinned, zero-network contract simulator.

    The returned manifest matches the check set accepted by
    ``worker_api.register_qualification_run_v2``. It is local evidence only;
    it never contacts a broker endpoint and cannot enable execution.
    """

    suite_version = "contract-test-qualification-v2"

    def __init__(
        self,
        *,
        contract_artifact_sha256: str = QUALIFIED_TOSS_OPENAPI_SHA256,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self.contract_artifact_sha256 = contract_artifact_sha256
        self.clock = clock
        self._brokers: list[ContractTestBroker] = []

    async def execute(
        self,
        *,
        started_at: datetime,
        completed_at: datetime | None = None,
    ) -> ContractQualificationReport:
        if completed_at is not None:
            _require_aware_window(started_at, completed_at)
        elif started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("contract_qualification_window_is_invalid")
        self._brokers = []
        runners: dict[ContractQualificationCheckId, CheckRunner] = {
            "cancel_lifecycle": self._verify_cancel_lifecycle,
            "create_lifecycle": self._verify_create_lifecycle,
            "fault_injection": self._verify_fault_injection,
            "ledger_invariants": self._verify_ledger_invariants,
            "production_order_network_zero": self._verify_network_boundary,
            "status_partial_terminal": self._verify_status_lifecycle,
        }
        checks = tuple(
            [await self._run_check(check_id, runners[check_id]) for check_id in _CHECK_IDS]
        )
        actual_completed_at = completed_at if completed_at is not None else self.clock()
        _require_aware_window(started_at, actual_completed_at)
        return ContractQualificationReport(
            suite_version=self.suite_version,
            openapi_sha256=self.contract_artifact_sha256,
            started_at=started_at,
            completed_at=actual_completed_at,
            result="pass" if all(check.status == "pass" for check in checks) else "fail",
            checks=checks,
        )

    async def _run_check(
        self,
        check_id: ContractQualificationCheckId,
        runner: CheckRunner,
    ) -> ContractQualificationCheck:
        try:
            metrics = dict(await runner())
            status: ContractQualificationStatus = "pass"
        except _QualificationCheckFailed as exc:
            metrics = {"reason_code": exc.reason_code}
            status = "fail"
        evidence_sha256 = _evidence_sha256(
            {
                "schema_version": 1,
                "suite_version": self.suite_version,
                "openapi_sha256": self.contract_artifact_sha256,
                "check_id": check_id,
                "status": status,
                "metrics": metrics,
            }
        )
        return ContractQualificationCheck(
            check_id=check_id,
            status=status,
            evidence_sha256=evidence_sha256,
            metrics=metrics,
        )

    async def _verify_create_lifecycle(self) -> Mapping[str, ContractMetricValue]:
        broker = self._broker(placement_outcomes=("sent",))
        request = _request("contract-qualification-create")
        created = await _qualified_call(
            broker.place_order(request),
            "create_lifecycle_create_failed",
        )
        replay = await _qualified_call(
            broker.place_order(request),
            "create_lifecycle_replay_failed",
        )
        if (
            created != replay
            or created.status != "sent"
            or created.provider_order_id is None
            or created.raw_summary.get("cumulative_quantity") != 0
        ):
            raise _QualificationCheckFailed("create_lifecycle_result_mismatch")
        return {
            "create_calls": broker.place_order_calls,
            "idempotent_replay": True,
            "provider_identity_present": True,
        }

    async def _verify_status_lifecycle(self) -> Mapping[str, ContractMetricValue]:
        broker = self._broker(
            placement_outcomes=("sent",),
            status_outcomes=("open", "partial_filled", "duplicate", "filled"),
        )
        request = _request("contract-qualification-status")
        created = await _qualified_call(
            broker.place_order(request),
            "status_lifecycle_create_failed",
        )
        if created.provider_order_id is None:
            raise _QualificationCheckFailed("status_lifecycle_identity_missing")
        observations = [
            await _qualified_call(
                broker.get_order_status(created.provider_order_id),
                "status_lifecycle_observation_failed",
            )
            for _ in range(4)
        ]
        if (
            [item.status for item in observations]
            != ["sent", "partial_filled", "partial_filled", "filled"]
            or observations[1] != observations[2]
            or observations[1].raw_summary.get("cumulative_quantity") != 2
            or observations[3].raw_summary.get("cumulative_quantity") != request.quantity
            or any(item.provider_order_id != created.provider_order_id for item in observations)
        ):
            raise _QualificationCheckFailed("status_lifecycle_result_mismatch")
        return {
            "status_calls": broker.get_order_status_calls,
            "partial_quantity": 2,
            "terminal_quantity": request.quantity,
            "duplicate_observation_noop": True,
        }

    async def _verify_cancel_lifecycle(self) -> Mapping[str, ContractMetricValue]:
        broker = self._broker(
            placement_outcomes=("sent",),
            status_outcomes=("partial_filled", "canceled"),
            cancel_outcomes=("canceled", "duplicate"),
        )
        request = _request("contract-qualification-cancel")
        created = await _qualified_call(
            broker.place_order(request),
            "cancel_lifecycle_create_failed",
        )
        if created.provider_order_id is None:
            raise _QualificationCheckFailed("cancel_lifecycle_identity_missing")
        partial = await _qualified_call(
            broker.get_order_status(created.provider_order_id),
            "cancel_lifecycle_partial_failed",
        )
        canceled = await _qualified_call(
            broker.cancel_order(created.provider_order_id),
            "cancel_lifecycle_cancel_failed",
        )
        replay = await _qualified_call(
            broker.cancel_order(created.provider_order_id),
            "cancel_lifecycle_replay_failed",
        )
        terminal = await _qualified_call(
            broker.get_order_status(created.provider_order_id),
            "cancel_lifecycle_terminal_failed",
        )
        if (
            partial.status != "partial_filled"
            or canceled != replay
            or canceled.original_provider_order_id != created.provider_order_id
            or terminal.status != "canceled"
        ):
            raise _QualificationCheckFailed("cancel_lifecycle_result_mismatch")
        return {
            "cancel_calls": broker.cancel_order_calls,
            "partial_before_cancel": True,
            "idempotent_replay": True,
            "terminal_canceled": True,
        }

    async def _verify_fault_injection(self) -> Mapping[str, ContractMetricValue]:
        scenarios = 0
        placement_faults: tuple[
            tuple[ContractPlacementOutcome, type[Exception]], ...
        ] = (
            ("timeout_before_accept", ProviderTimeoutError),
            ("timeout_after_accept", ProviderTimeoutError),
            ("http_5xx", ProviderUnavailableError),
            ("malformed", ProviderSchemaError),
            ("unknown_after_accept", ProviderUnknownError),
        )
        for index, (placement_outcome, expected_error) in enumerate(placement_faults):
            broker = self._broker(placement_outcomes=(placement_outcome,))
            await _expect_provider_error(
                broker.place_order(_request(f"contract-placement-fault-{index}")),
                expected_error,
            )
            scenarios += 1

        status_faults: tuple[tuple[ContractStatusOutcome, type[Exception]], ...] = (
            ("timeout", ProviderTimeoutError),
            ("http_5xx", ProviderUnavailableError),
            ("malformed", ProviderSchemaError),
        )
        for index, (status_outcome, expected_error) in enumerate(status_faults):
            broker = self._broker(
                placement_outcomes=("sent",),
                status_outcomes=(status_outcome,),
            )
            created = await broker.place_order(_request(f"contract-status-fault-{index}"))
            if created.provider_order_id is None:
                raise _QualificationCheckFailed("fault_status_identity_missing")
            await _expect_provider_error(
                broker.get_order_status(created.provider_order_id),
                expected_error,
            )
            scenarios += 1

        special_status = self._broker(
            placement_outcomes=("sent",),
            status_outcomes=("unknown", "mismatched_identity"),
        )
        created = await special_status.place_order(_request("contract-status-special"))
        if created.provider_order_id is None:
            raise _QualificationCheckFailed("fault_status_identity_missing")
        unknown = await special_status.get_order_status(created.provider_order_id)
        mismatch = await special_status.get_order_status(created.provider_order_id)
        if (
            unknown.status != "unknown_requires_manual_check"
            or mismatch.provider_order_id == created.provider_order_id
        ):
            raise _QualificationCheckFailed("fault_status_safe_state_missing")
        scenarios += 2

        cancel_faults: tuple[tuple[ContractCancelOutcome, type[Exception]], ...] = (
            ("timeout_before_accept", ProviderTimeoutError),
            ("timeout_after_accept", ProviderTimeoutError),
            ("http_5xx", ProviderUnavailableError),
            ("malformed", ProviderSchemaError),
            ("unknown_after_accept", ProviderUnknownError),
        )
        for index, (cancel_outcome, expected_error) in enumerate(cancel_faults):
            broker = self._broker(
                placement_outcomes=("sent",),
                cancel_outcomes=(cancel_outcome,),
            )
            created = await broker.place_order(_request(f"contract-cancel-fault-{index}"))
            if created.provider_order_id is None:
                raise _QualificationCheckFailed("fault_cancel_identity_missing")
            await _expect_provider_error(
                broker.cancel_order(created.provider_order_id),
                expected_error,
            )
            scenarios += 1

        unqualified = ContractTestBroker(contract_artifact_sha256="0" * 64)
        self._brokers.append(unqualified)
        await _expect_provider_error(
            unqualified.place_order(_request("contract-artifact-mismatch")),
            ProviderUnavailableError,
        )
        scenarios += 1
        if scenarios != 16:
            raise _QualificationCheckFailed("fault_scenario_count_mismatch")
        return {
            "scenario_count": scenarios,
            "ambiguous_states_fail_closed": True,
            "artifact_mismatch_blocked": True,
        }

    async def _verify_network_boundary(self) -> Mapping[str, ContractMetricValue]:
        if not self._brokers:
            raise _QualificationCheckFailed("network_boundary_no_simulator_evidence")
        if any(
            broker.network_enabled is not False
            or broker.production_order_capable is not False
            or broker.execution_environment != "contract_test"
            for broker in self._brokers
        ):
            raise _QualificationCheckFailed("network_boundary_not_fail_closed")
        return {
            "request_count": 0,
        }

    async def _verify_ledger_invariants(self) -> Mapping[str, ContractMetricValue]:
        kernel, intent, cost_schedule, execution_evidence = (
            await _qualification_contract_context()
        )
        broker = self._broker(placement_outcomes=("filled",))
        service = ExecutionService(
            broker,
            InMemoryRepository(BotSettings()),
            RiskService(),
        )
        await _qualified_call(
            service.dispatch_contract_test_order(
                intent,
                kernel,
                cost_schedule=cost_schedule,
                execution_evidence=execution_evidence,
                now=intent.eligible_at,
            ),
            "ledger_dispatch_failed",
        )
        snapshot = await kernel.account_snapshot(intent.account_id)
        transactions = await kernel.accounting_transactions_for(intent.id)
        if (
            len(transactions) != 1
            or transactions[0].total_debit_krw
            != transactions[0].total_credit_krw
            or snapshot.quantity_for(intent.symbol) != intent.quantity
            or snapshot.reserved_cash_krw != 0
        ):
            raise _QualificationCheckFailed("ledger_projection_or_journal_mismatch")

        identity_kernel, identity_intent, _, _ = await _qualification_contract_context(
            account_id="contract-qualification-identity"
        )
        await identity_kernel.mark_dispatch_started(
            identity_intent,
            now=identity_intent.eligible_at,
        )
        await identity_kernel.record_execution_observation(
            identity_intent,
            ExecutionObservation.create(
                intent_id=identity_intent.id,
                sequence=1,
                status="open",
                observed_at=identity_intent.eligible_at,
                provider_order_id="contract-identity-a",
                provider_execution_id=None,
                cumulative_quantity=0,
                cumulative_gross_krw=0,
                cumulative_commission_krw=0,
                cumulative_tax_krw=0,
            ),
            now=identity_intent.eligible_at,
        )
        try:
            await identity_kernel.record_execution_observation(
                identity_intent,
                ExecutionObservation.create(
                    intent_id=identity_intent.id,
                    sequence=2,
                    status="open",
                    observed_at=identity_intent.eligible_at + timedelta(seconds=1),
                    provider_order_id="contract-identity-b",
                    provider_execution_id=None,
                    cumulative_quantity=0,
                    cumulative_gross_krw=0,
                    cumulative_commission_krw=0,
                    cumulative_tax_krw=0,
                ),
                now=identity_intent.eligible_at + timedelta(seconds=1),
            )
        except ExecutionInvariantError as exc:
            if exc.safe_message != "provider_order_identity_mismatch":
                raise _QualificationCheckFailed(
                    "ledger_identity_failure_reason_mismatch"
                ) from exc
        else:
            raise _QualificationCheckFailed("ledger_identity_change_was_accepted")
        return {
            "balanced_transaction_count": 1,
            "position_quantity": intent.quantity,
            "provider_identity_change_blocked": True,
            "projection_backed_by_journal": True,
        }

    def _broker(
        self,
        placement_outcomes: tuple[ContractPlacementOutcome, ...] = (),
        *,
        status_outcomes: tuple[ContractStatusOutcome, ...] = (),
        cancel_outcomes: tuple[ContractCancelOutcome, ...] = (),
    ) -> ContractTestBroker:
        broker = ContractTestBroker(
            placement_outcomes,
            status_outcomes=status_outcomes,
            cancel_outcomes=cancel_outcomes,
            contract_artifact_sha256=self.contract_artifact_sha256,
        )
        self._brokers.append(broker)
        return broker


async def _qualified_call[T](value: Awaitable[T], reason_code: str) -> T:
    try:
        return await value
    except (
        ProviderSchemaError,
        ProviderTimeoutError,
        ProviderUnavailableError,
        ProviderUnknownError,
    ) as exc:
        raise _QualificationCheckFailed(reason_code) from exc


async def _expect_provider_error(
    value: Awaitable[object],
    expected_error: type[Exception],
) -> None:
    try:
        await value
    except expected_error:
        return
    except Exception as exc:
        raise _QualificationCheckFailed("fault_error_type_mismatch") from exc
    raise _QualificationCheckFailed("fault_scenario_did_not_fail")


def _request(idempotency_key: str) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        symbol="005930",
        side="buy",
        amount_krw=40_000,
        idempotency_key=idempotency_key,
        quantity=4,
        limit_price_krw=10_000,
    )


async def _qualification_contract_context(
    *,
    account_id: str = "contract-qualification-account",
) -> tuple[
    InMemoryExecutionKernelV2,
    ExecutionIntent,
    ExecutionCostSchedule,
    PaperExecutionEvidence,
]:
    now = datetime(2026, 7, 15, 1, 0, 30, tzinfo=UTC)
    expires_at = now.replace(hour=8, minute=0, second=0)
    kernel = InMemoryExecutionKernelV2("contract_test")
    await kernel.configure_account(account_id, cash_krw=1_000_000)
    await kernel.replace_gate(
        ExecutionGate(
            account_id=account_id,
            environment="contract_test",
            enabled=True,
            control_epoch=1,
            effective_at=now,
            expires_at=expires_at + timedelta(hours=1),
        )
    )
    lease = await kernel.acquire_lease(
        account_id=account_id,
        holder_id="contract-qualification-worker",
        now=now,
        ttl=timedelta(hours=8),
    )
    cost_schedule = ExecutionCostSchedule(
        version="contract-qualification-cost-v1",
        effective_from=now - timedelta(days=1),
        effective_until=expires_at + timedelta(days=1),
        evidence_sha256="a" * 64,
        settlement_days=0,
        settlement_evidence_sha256="b" * 64,
        buy_commission_rate=Decimal("0.001"),
        sell_commission_rate=Decimal("0.001"),
        sell_tax_rate=Decimal("0.002"),
    )
    intent = ExecutionIntent.create(
        account_id=account_id,
        environment="contract_test",
        decision_id="11111111-1111-4111-8111-111111111111",
        risk_result_id="22222222-2222-4222-8222-222222222222",
        decision_feature_sha256="c" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=now + timedelta(hours=1),
        strategy_version_id="contract-qualification-strategy-v1",
        symbol="005930",
        side="buy",
        quantity=4,
        limit_price_krw=10_000,
        decision_at=now,
        signal_valid_from=now - timedelta(minutes=1),
        signal_valid_until=expires_at,
        execution_policy_version="contract-qualification-policy-v1",
        cost_schedule=cost_schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id=lease.holder_id,
        lease_fencing_token=lease.fencing_token,
    )
    execution_evidence = PaperExecutionEvidence(
        version="contract-qualification-evidence-v1",
        execution_policy_version=intent.execution_policy_version,
        effective_from=now - timedelta(days=1),
        effective_until=expires_at + timedelta(days=1),
        tick_rule_version="contract-qualification-tick-v1",
        tick_size_krw=1,
        tick_rule_evidence_sha256="d" * 64,
        volume_source="contract_qualification_fixture",
        volume_unit="shares",
        volume_evidence_sha256="e" * 64,
        corporate_action_status="not_required",
        corporate_action_evidence_sha256="f" * 64,
        market_calendar_version="contract-qualification-calendar-v1",
        market_calendar_status="open_sessions_verified",
        market_calendar_evidence_sha256="1" * 64,
        open_session_dates=tuple(
            intent.eligible_at.date() + timedelta(days=offset) for offset in range(4)
        ),
    )
    assert await kernel.reserve_intent(intent, now=now)
    return kernel, intent, cost_schedule, execution_evidence


def _evidence_sha256(value: Mapping[str, object]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_aware_window(started_at: datetime, completed_at: datetime) -> None:
    if (
        started_at.tzinfo is None
        or started_at.utcoffset() is None
        or completed_at.tzinfo is None
        or completed_at.utcoffset() is None
        or completed_at < started_at
    ):
        raise ValueError("contract_qualification_window_is_invalid")

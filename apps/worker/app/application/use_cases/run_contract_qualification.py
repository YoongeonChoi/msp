from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.adapters.broker.contract_test_broker import (
    QUALIFIED_TOSS_OPENAPI_SHA256,
    ContractCancelOutcome,
    ContractPlacementOutcome,
    ContractStatusOutcome,
    ContractTestBroker,
)
from app.application.ports.broker_port import BrokerOrderRequest
from app.domain.common.errors import (
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)

ContractQualificationCheckId = Literal[
    "cancel_lifecycle",
    "create_lifecycle",
    "fault_injection",
    "production_order_network_zero",
    "status_partial_terminal",
]
ContractQualificationStatus = Literal["pass", "fail"]
ContractMetricValue = bool | int | str

_CHECK_IDS: tuple[ContractQualificationCheckId, ...] = (
    "cancel_lifecycle",
    "create_lifecycle",
    "fault_injection",
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


class RunContractQualification:
    """Exercise the pinned, zero-network contract simulator.

    The returned manifest matches the check set accepted by
    ``worker_api.register_qualification_run_v1``. It is local evidence only;
    it never contacts a broker endpoint and cannot enable execution.
    """

    suite_version = "contract-test-qualification-v1"

    def __init__(
        self,
        *,
        contract_artifact_sha256: str = QUALIFIED_TOSS_OPENAPI_SHA256,
    ) -> None:
        self.contract_artifact_sha256 = contract_artifact_sha256
        self._brokers: list[ContractTestBroker] = []

    async def execute(
        self,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> ContractQualificationReport:
        _require_aware_window(started_at, completed_at)
        self._brokers = []
        runners: dict[ContractQualificationCheckId, CheckRunner] = {
            "cancel_lifecycle": self._verify_cancel_lifecycle,
            "create_lifecycle": self._verify_create_lifecycle,
            "fault_injection": self._verify_fault_injection,
            "production_order_network_zero": self._verify_network_boundary,
            "status_partial_terminal": self._verify_status_lifecycle,
        }
        checks = tuple(
            [await self._run_check(check_id, runners[check_id]) for check_id in _CHECK_IDS]
        )
        return ContractQualificationReport(
            suite_version=self.suite_version,
            openapi_sha256=self.contract_artifact_sha256,
            started_at=started_at,
            completed_at=completed_at,
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
            "simulator_instance_count": len(self._brokers),
            "execution_transport": "local_contract_simulator",
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

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Never, cast

import pytest

from app.application.ports.durable_scheduler_port import (
    SchedulerMutationOutcomeUnknownError,
    SchedulerTransitionRejectedError,
)
from app.application.ports.persistence_authority import (
    persistence_authority_fingerprint,
)
from app.application.services import scheduler_invocation_deadline as deadline_module
from app.application.services.scheduler_invocation_deadline import (
    SCHEDULER_DEADLINE_FAILURES,
    FailStop,
    SchedulerInvocationBinding,
    SchedulerInvocationDeadlineExceeded,
    SchedulerInvocationPermit,
    SchedulerInvocationPermitRevoked,
    SchedulerInvocationSettlementWindowExceeded,
    require_scheduler_invocation_permit,
)
from app.application.use_cases import run_durable_scheduler as scheduler_module
from app.application.use_cases.apply_operation_commands import OperationCommandRunResult
from app.application.use_cases.dispatch_alert_outbox import AlertOutboxDispatchResult
from app.application.use_cases.mature_cash_settlements import CashSettlementRunResult
from app.application.use_cases.reconcile_execution_v2 import (
    ExecutionReconciliationRunResult,
)
from app.application.use_cases.run_durable_scheduler import (
    SCHEDULER_CONVERGENCE_ORDER,
    SCHEDULER_RESULT_VALIDATORS,
    ConvergeDurableSchedulerDefinitions,
    DurableSchedulerConvergenceResult,
    RunDurableSchedulerOnce,
    SchedulerJobBinding,
    SchedulerJobHandler,
    SchedulerRetryableError,
    SchedulerRunFailedError,
    validate_operation_commands_scheduler_result,
    validate_operation_execution_scheduler_result,
    validate_operation_outbox_scheduler_result,
    validate_operation_reconciliation_scheduler_result,
    validate_operation_settlement_scheduler_result,
)
from app.application.use_cases.run_execution_supervisor_v2 import (
    ExecutionSupervisorV2RunResult,
)
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    SCHEDULER_EFFECTFUL_JOB_KEYS,
    SCHEDULER_JOB_KEYS,
    ScheduledJobClaimReceiptV1,
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobConvergenceDefinitionV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    ScheduledJobLeaseV1,
    ScheduledJobRunV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerJobKey,
    scheduler_result_sha256,
    scheduler_retry_delay,
)

NOW = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
ACCOUNT_ID = "paper-primary"
HOLDER_ID = "44444444-4444-4444-8444-444444444444"
RELEASE_SHA = "a" * 40
OTHER_RELEASE_SHA = "c" * 40
PERSISTENCE_AUTHORITY = persistence_authority_fingerprint(
    namespace="supabase-worker-api",
    origin="http://127.0.0.1:54321",
    profile="worker_api",
)
OTHER_PERSISTENCE_AUTHORITY = persistence_authority_fingerprint(
    namespace="supabase-worker-api",
    origin="http://127.0.0.1:54322",
    profile="worker_api",
)
RUN_IDS: Mapping[SchedulerJobKey, str] = {
    "operations.commands": "11111111-1111-4111-8111-111111111111",
    "operations.execution": "22222222-2222-4222-8222-222222222222",
    "operations.settlement": "33333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "55555555-5555-4555-8555-555555555555",
    "operations.outbox": "66666666-6666-4666-8666-666666666666",
}
LEASE_TOKENS: Mapping[SchedulerJobKey, str] = {
    "operations.commands": "71111111-1111-4111-8111-111111111111",
    "operations.execution": "72222222-2222-4222-8222-222222222222",
    "operations.settlement": "73333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "75555555-5555-4555-8555-555555555555",
    "operations.outbox": "76666666-6666-4666-8666-666666666666",
}
DEFINITION_IDS: Mapping[SchedulerJobKey, str] = {
    "operations.commands": "81111111-1111-4111-8111-111111111111",
    "operations.execution": "82222222-2222-4222-8222-222222222222",
    "operations.settlement": "83333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "85555555-5555-4555-8555-555555555555",
    "operations.outbox": "86666666-6666-4666-8666-666666666666",
}

_Clock = deadline_module._SchedulerTestMonotonicClock


class _FailStopTriggered(BaseException):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _HandlerFailStop(BaseException):
    pass


def _raise_fail_stop(reason: str) -> Never:
    raise _FailStopTriggered(reason)


def _must_not_fail_stop(reason: str) -> Never:
    raise AssertionError(f"unexpected fail-stop: {reason}")


class _Handler:
    def __init__(
        self,
        *,
        result: object | None = None,
        failure: BaseException | None = None,
        on_call: Callable[
            [SchedulerInvocationPermit, SchedulerInvocationBinding], None
        ]
        | None = None,
        validate_permit: bool = True,
        wait_for: asyncio.Event | None = None,
        entered: asyncio.Event | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.on_call = on_call
        self.validate_permit = validate_permit
        self.wait_for = wait_for
        self.entered = entered
        self.calls = 0
        self.permits: list[SchedulerInvocationPermit] = []
        self.bindings: list[SchedulerInvocationBinding] = []

    async def __call__(
        self,
        permit: SchedulerInvocationPermit,
        invocation_binding: SchedulerInvocationBinding,
    ) -> object:
        self.calls += 1
        self.permits.append(permit)
        self.bindings.append(invocation_binding)
        if self.validate_permit:
            require_scheduler_invocation_permit(
                permit,
                expected_binding=invocation_binding,
            )
        if self.entered is not None:
            self.entered.set()
        if self.on_call is not None:
            self.on_call(permit, invocation_binding)
        if self.failure is not None:
            raise self.failure
        if self.wait_for is not None:
            await self.wait_for.wait()
        return self.result


CompletionMutator = Callable[
    [ScheduledJobCompletionReceiptV1], ScheduledJobCompletionReceiptV1
]
FailureMutator = Callable[
    [ScheduledJobFailureReceiptV1], ScheduledJobFailureReceiptV1
]


class _SchedulerPortStub:
    def __init__(
        self,
        claim: ScheduledJobClaimV1 | None,
        *,
        convergence_by_key: Mapping[
            SchedulerJobKey,
            SchedulerDefinitionConvergenceReceiptV1,
        ]
        | None = None,
    ) -> None:
        self.release_sha = RELEASE_SHA
        self.persistence_authority = PERSISTENCE_AUTHORITY
        self.claim = claim
        self.convergence_by_key = dict(convergence_by_key or {})
        self.claim_calls = 0
        self.convergence_calls: list[SchedulerJobKey] = []
        self.completion_calls: list[
            tuple[ScheduledJobClaimV1, WorkerLease, str]
        ] = []
        self.failure_calls: list[
            tuple[ScheduledJobClaimV1, WorkerLease, str, str, bool]
        ] = []
        self.completion_error: BaseException | None = None
        self.failure_error: BaseException | None = None
        self.completion_mutator: CompletionMutator | None = None
        self.failure_mutator: FailureMutator | None = None
        self.after_rpc: Callable[[Literal["complete", "fail"]], None] | None = None

    async def claim_due_job(
        self,
        *,
        outer_lease: WorkerLease,
    ) -> ScheduledJobClaimReceiptV1:
        assert outer_lease.account_id == ACCOUNT_ID
        self.claim_calls += 1
        observed_at = self.claim.observed_at if self.claim is not None else NOW
        return ScheduledJobClaimReceiptV1(
            claim=self.claim,
            observed_at=observed_at,
        )

    async def converge_job_definition(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
    ) -> SchedulerDefinitionConvergenceReceiptV1:
        assert outer_lease.account_id == ACCOUNT_ID
        self.convergence_calls.append(definition.job_key)
        return self.convergence_by_key.get(
            definition.job_key,
            _convergence_receipt(definition),
        )

    async def complete_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        result_sha256: str,
    ) -> ScheduledJobCompletionReceiptV1:
        self.completion_calls.append((claim, outer_lease, result_sha256))
        if self.completion_error is not None:
            raise self.completion_error
        receipt = ScheduledJobCompletionReceiptV1(
            run_id=claim.run.run_id,
            run_revision=claim.run.revision + 1,
            attempt_count=claim.run.attempt_count,
            next_attempt_at=None,
            failure_reason_code=None,
            result_sha256=result_sha256,
            observed_at=claim.observed_at + timedelta(seconds=1),
        )
        if self.completion_mutator is not None:
            receipt = self.completion_mutator(receipt)
        if self.after_rpc is not None:
            self.after_rpc("complete")
        return receipt

    async def fail_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        failure_reason_code: str,
        failure_sha256: str,
        retryable: bool,
    ) -> ScheduledJobFailureReceiptV1:
        self.failure_calls.append(
            (
                claim,
                outer_lease,
                failure_reason_code,
                failure_sha256,
                retryable,
            )
        )
        if self.failure_error is not None:
            raise self.failure_error
        retry_wait = retryable and claim.run.attempt_count < claim.definition.max_attempts
        transition_at = claim.observed_at + timedelta(microseconds=500_000)
        receipt = ScheduledJobFailureReceiptV1(
            run_id=claim.run.run_id,
            run_revision=claim.run.revision + 1,
            attempt_count=claim.run.attempt_count,
            state="retry_wait" if retry_wait else "dead_letter",
            failure_reason_code=failure_reason_code,
            result_sha256=failure_sha256,
            next_attempt_at=(
                transition_at
                + scheduler_retry_delay(claim.definition, claim.run.attempt_count)
                if retry_wait
                else None
            ),
            observed_at=claim.observed_at + timedelta(seconds=1),
        )
        if self.failure_mutator is not None:
            receipt = self.failure_mutator(receipt)
        if self.after_rpc is not None:
            self.after_rpc("fail")
        return receipt


class _RuntimeStub:
    def __init__(self, port: _SchedulerPortStub) -> None:
        self.scheduler_port: Any = port
        self.account_id = ACCOUNT_ID
        self.holder_id = HOLDER_ID
        self.release_sha = RELEASE_SHA
        self.persistence_authority = PERSISTENCE_AUTHORITY
        self.outer_lease = _outer_lease()
        self.integrity_checks = 0
        self.on_integrity_check: Callable[[], None] | None = None

    def assert_intact(self) -> None:
        self.integrity_checks += 1
        if self.on_integrity_check is not None:
            self.on_integrity_check()

    def current_outer_lease(self) -> WorkerLease:
        return self.outer_lease


def test_scheduler_core_constructor_and_handler_contract() -> None:
    run_signature = inspect.signature(RunDurableSchedulerOnce.__init__)
    assert tuple(run_signature.parameters) == (
        "self",
        "runtime",
        "bindings",
        "fail_stop",
    )
    assert run_signature.parameters["fail_stop"].kind is inspect.Parameter.KEYWORD_ONLY

    convergence_signature = inspect.signature(
        ConvergeDurableSchedulerDefinitions.__init__
    )
    assert tuple(convergence_signature.parameters) == (
        "self",
        "runtime",
        "definitions",
        "bindings",
        "fail_stop",
    )
    assert (
        convergence_signature.parameters["fail_stop"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )

    handler_signature = inspect.signature(SchedulerJobHandler.__call__)
    assert tuple(handler_signature.parameters) == (
        "self",
        "permit",
        "invocation_binding",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "raw_handlers",
        "key_mismatch",
        "validator_mismatch",
        "runtime_account_mismatch",
        "runtime_authority_mismatch",
    ],
)
def test_binding_registry_is_exact_and_runtime_scoped(mutation: str) -> None:
    port = _SchedulerPortStub(None)
    runtime = _RuntimeStub(port)
    bindings: Any = _bindings()
    if mutation == "missing":
        bindings.pop("operations.outbox")
    elif mutation == "extra":
        bindings[cast(Any, "operations.unknown")] = bindings["operations.commands"]
    elif mutation == "raw_handlers":
        bindings = {key: binding.handler for key, binding in bindings.items()}
    else:
        original = bindings["operations.commands"]
        values: dict[str, object] = {
            "job_key": original.job_key,
            "handler": original.handler,
            "result_validator": original.result_validator,
        }
        if mutation == "key_mismatch":
            values["job_key"] = "operations.outbox"
        elif mutation == "validator_mismatch":
            values["result_validator"] = _passthrough_result
        elif mutation == "runtime_account_mismatch":
            runtime.account_id = "paper-other"
        elif mutation == "runtime_authority_mismatch":
            runtime.persistence_authority = OTHER_PERSISTENCE_AUTHORITY
        bindings["operations.commands"] = SchedulerJobBinding(**cast(Any, values))

    with pytest.raises(SchedulerInvariantError):
        RunDurableSchedulerOnce(
            cast(Any, runtime),
            bindings,
            fail_stop=_raise_fail_stop,
        )

    assert port.claim_calls == 0


@pytest.mark.parametrize(
    "validator",
    [
        validate_operation_commands_scheduler_result,
        validate_operation_execution_scheduler_result,
        validate_operation_settlement_scheduler_result,
        validate_operation_reconciliation_scheduler_result,
        validate_operation_outbox_scheduler_result,
    ],
)
def test_result_validators_reject_untyped_payloads(validator: Any) -> None:
    with pytest.raises(SchedulerInvariantError, match="result_type_is_invalid"):
        validator({})


@pytest.mark.parametrize(
    ("validator", "result"),
    [
        (
            validate_operation_commands_scheduler_result,
            OperationCommandRunResult(claimed=1, applied=0, failed=1),
        ),
        (
            validate_operation_execution_scheduler_result,
            ExecutionSupervisorV2RunResult(1, 0, 0, 0, 0, 0, 0),
        ),
        (
            validate_operation_settlement_scheduler_result,
            CashSettlementRunResult(1, 0, 0, 0, 0, 0),
        ),
        (
            validate_operation_reconciliation_scheduler_result,
            ExecutionReconciliationRunResult(1, 0, 0, 0),
        ),
        (
            validate_operation_outbox_scheduler_result,
            AlertOutboxDispatchResult(1, 0, 0),
        ),
    ],
)
def test_result_validators_reject_failed_or_inconsistent_accounting(
    validator: Any,
    result: object,
) -> None:
    with pytest.raises(SchedulerInvariantError):
        validator(result)


def test_result_validators_accept_exact_typed_business_outcomes() -> None:
    results: Mapping[SchedulerJobKey, object] = {
        key: _stage_result(key) for key in cast(frozenset[SchedulerJobKey], SCHEDULER_JOB_KEYS)
    }
    for key, result in results.items():
        assert SCHEDULER_RESULT_VALIDATORS[key](result) is result


async def test_normal_success_uses_exact_permit_and_completes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    handler = _Handler(result=OperationCommandRunResult(claimed=2, applied=2))
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={"operations.commands": handler},
    )

    result = await runner.run_once()

    assert result.outcome == "succeeded"
    assert result.handler_result == OperationCommandRunResult(claimed=2, applied=2)
    assert result.result_sha256 == scheduler_result_sha256(result.handler_result)
    assert handler.calls == 1
    assert handler.bindings[0].run_id == RUN_IDS["operations.commands"]
    assert len(port.completion_calls) == 1
    assert port.completion_calls[0][2] == result.result_sha256
    assert port.failure_calls == []


async def test_normal_idle_has_no_dispatch_or_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    handler = _Handler(result=OperationCommandRunResult(0, 0))
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=None,
        handlers={"operations.commands": handler},
    )

    result = await runner.run_once()

    assert result.outcome == "idle"
    assert port.claim_calls == 1
    assert handler.calls == 0
    assert port.completion_calls == []
    assert port.failure_calls == []


@pytest.mark.parametrize(
    "claim_options",
    [
        {"enabled": False},
        {"max_attempts": 4},
        {"job_key": "operations.execution", "max_attempts": 2},
    ],
)
async def test_normal_claim_policy_rejects_before_handler(
    monkeypatch: pytest.MonkeyPatch,
    claim_options: Mapping[str, object],
) -> None:
    clock = _install_clock(monkeypatch)
    claim = _claim(**cast(Any, claim_options))
    handler = _Handler(result=_stage_result(claim.job_key))
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=claim,
        handlers={claim.job_key: handler},
    )

    with pytest.raises(SchedulerInvariantError):
        await runner.run_once()

    assert handler.calls == 0
    assert port.completion_calls == []
    assert port.failure_calls == []


@pytest.mark.parametrize(
    ("job_key", "reason_code", "retryable"),
    [
        ("operations.commands", "command_poll_retryable", True),
        ("operations.reconciliation", "reconciliation_poll_retryable", True),
        ("operations.outbox", "outbox_poll_retryable", True),
        ("operations.execution", "execution_deadline_effect_unknown", False),
        ("operations.settlement", "settlement_deadline_effect_unknown", False),
    ],
)
async def test_actual_deadline_failure_policy_matrix(
    monkeypatch: pytest.MonkeyPatch,
    job_key: SchedulerJobKey,
    reason_code: str,
    retryable: bool,
) -> None:
    clock = _install_clock(monkeypatch)
    handler = _Handler(result=_stage_result(job_key))
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(job_key=job_key, lease_seconds=5),
        handlers={job_key: handler},
    )

    if retryable:
        result = await runner.run_once()
        assert result.outcome == "retry_wait"
    else:
        with pytest.raises(SchedulerRunFailedError) as captured:
            await runner.run_once()
        assert captured.value.reason_code == reason_code
        assert captured.value.failure_receipt.failure_reason_code == reason_code

    assert handler.calls == 0
    assert port.completion_calls == []
    assert len(port.failure_calls) == 1
    assert port.failure_calls[0][2] == reason_code
    assert port.failure_calls[0][4] is retryable
    assert SCHEDULER_DEADLINE_FAILURES[job_key].reason_code == reason_code


async def test_forged_handler_deadline_is_unknown_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    handler = _Handler(
        failure=SchedulerInvocationDeadlineExceeded("operations.commands")
    )
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={"operations.commands": handler},
    )

    with pytest.raises(SchedulerRunFailedError) as captured:
        await runner.run_once()

    assert captured.value.reason_code == "scheduler_handler_unknown_failure"
    assert (
        captured.value.failure_receipt.failure_reason_code
        == "scheduler_handler_unknown_failure"
    )
    assert len(port.failure_calls) == 1
    assert port.failure_calls[0][2] == "scheduler_handler_unknown_failure"
    assert port.failure_calls[0][4] is False
    assert port.completion_calls == []


@pytest.mark.parametrize(
    ("job_key", "reason_code"),
    [
        ("operations.commands", "command_poll_retryable"),
        ("operations.reconciliation", "reconciliation_poll_retryable"),
        ("operations.outbox", "outbox_poll_retryable"),
    ],
)
async def test_allowed_retryable_handler_failure_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    job_key: SchedulerJobKey,
    reason_code: str,
) -> None:
    clock = _install_clock(monkeypatch)
    handler = _Handler(failure=SchedulerRetryableError(reason_code))
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(job_key=job_key),
        handlers={job_key: handler},
    )

    result = await runner.run_once()

    assert result.outcome == "retry_wait"
    assert len(port.failure_calls) == 1
    assert port.failure_calls[0][2] == reason_code
    assert port.failure_calls[0][4] is True
    assert port.completion_calls == []


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (
            SchedulerRetryableError("outbox_poll_retryable"),
            "scheduler_retry_classification_not_allowed",
        ),
        (RuntimeError("terminal"), "scheduler_handler_unknown_failure"),
    ],
)
async def test_handler_failure_classification_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_reason: str,
) -> None:
    clock = _install_clock(monkeypatch)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={"operations.commands": _Handler(failure=failure)},
    )

    with pytest.raises(SchedulerRunFailedError) as captured:
        await runner.run_once()

    assert captured.value.reason_code == expected_reason
    assert captured.value.failure_receipt.failure_reason_code == expected_reason
    assert len(port.failure_calls) == 1
    assert port.failure_calls[0][2] == expected_reason
    assert port.failure_calls[0][4] is False
    assert port.completion_calls == []


async def test_invalid_handler_result_is_terminal_and_never_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={
            "operations.commands": _Handler(
                result=OperationCommandRunResult(claimed=1, applied=0, failed=1)
            )
        },
    )

    with pytest.raises(SchedulerRunFailedError) as captured:
        await runner.run_once()

    assert captured.value.reason_code == "scheduler_handler_result_rejected"
    assert (
        captured.value.failure_receipt.failure_reason_code
        == "scheduler_handler_result_rejected"
    )
    assert len(port.failure_calls) == 1
    assert port.failure_calls[0][2] == "scheduler_handler_result_rejected"
    assert port.failure_calls[0][4] is False
    assert port.completion_calls == []


async def test_retryable_last_attempt_dead_letters_without_reclassification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    claim = _claim(attempt_count=3)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=claim,
        handlers={
            "operations.commands": _Handler(
                failure=SchedulerRetryableError("command_poll_retryable")
            )
        },
    )

    result = await runner.run_once()

    assert result.outcome == "dead_letter"
    assert port.failure_calls[0][2] == "command_poll_retryable"
    assert port.failure_calls[0][4] is True
    assert port.completion_calls == []


@pytest.mark.parametrize(
    "failure",
    [
        _HandlerFailStop("handler_fail_stop"),
        SchedulerInvocationPermitRevoked("deadline"),
        asyncio.CancelledError(),
    ],
)
async def test_handler_control_flow_failures_never_settle(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    clock = _install_clock(monkeypatch)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={
            "operations.commands": _Handler(
                failure=failure,
                validate_permit=False,
            )
        },
    )

    with pytest.raises(type(failure)):
        await runner.run_once()

    assert port.completion_calls == []
    assert port.failure_calls == []


async def test_external_cancellation_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={
            "operations.commands": _Handler(
                result=OperationCommandRunResult(0, 0),
                entered=entered,
                wait_for=release,
            )
        },
    )
    task = asyncio.create_task(runner.run_once())
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert port.completion_calls == []
    assert port.failure_calls == []


async def test_late_settlement_start_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)

    def exceed_every_cutoff(
        _permit: SchedulerInvocationPermit,
        _binding: SchedulerInvocationBinding,
    ) -> None:
        clock.value = 126.0

    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={
            "operations.commands": _Handler(
                result=OperationCommandRunResult(0, 0),
                on_call=exceed_every_cutoff,
            )
        },
    )

    with pytest.raises(SchedulerInvocationSettlementWindowExceeded):
        await runner.run_once()

    assert port.completion_calls == []
    assert port.failure_calls == []


@pytest.mark.parametrize(
    "error",
    [SchedulerMutationOutcomeUnknownError(), SchedulerTransitionRejectedError()],
)
async def test_completion_unknown_or_rejected_never_fails(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    clock = _install_clock(monkeypatch)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
    )
    port.completion_error = error

    with pytest.raises(type(error)):
        await runner.run_once()

    assert len(port.completion_calls) == 1
    assert port.failure_calls == []


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (
            SchedulerRetryableError("command_poll_retryable"),
            SchedulerMutationOutcomeUnknownError(),
        ),
        (
            SchedulerRetryableError("command_poll_retryable"),
            SchedulerTransitionRejectedError(),
        ),
        (RuntimeError("terminal"), SchedulerMutationOutcomeUnknownError()),
        (RuntimeError("terminal"), SchedulerTransitionRejectedError()),
    ],
)
async def test_failure_unknown_or_rejected_never_completes(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    error: Exception,
) -> None:
    clock = _install_clock(monkeypatch)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={"operations.commands": _Handler(failure=failure)},
    )
    port.failure_error = error

    with pytest.raises(type(error)):
        await runner.run_once()

    assert len(port.failure_calls) == 1
    assert port.completion_calls == []


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: replace(
            value,
            run_id="99999999-9999-4999-8999-999999999999",
        ),
        lambda value: replace(value, run_revision=value.run_revision + 1),
        lambda value: replace(value, attempt_count=value.attempt_count + 1),
        lambda value: replace(value, result_sha256="f" * 64),
        lambda value: replace(value, observed_at=NOW - timedelta(seconds=1)),
    ],
)
async def test_completion_receipt_binding_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    mutator: CompletionMutator,
) -> None:
    clock = _install_clock(monkeypatch)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
    )
    port.completion_mutator = mutator

    with pytest.raises(
        SchedulerMutationOutcomeUnknownError,
        match="scheduler_success_response_is_invalid",
    ):
        await runner.run_once()

    assert len(port.completion_calls) == 1
    assert port.failure_calls == []


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: replace(
            value,
            run_id="99999999-9999-4999-8999-999999999999",
        ),
        lambda value: replace(value, run_revision=value.run_revision + 1),
        lambda value: replace(value, attempt_count=value.attempt_count + 1),
        lambda value: replace(value, failure_reason_code="wrong_reason"),
        lambda value: replace(value, result_sha256="f" * 64),
        lambda value: replace(value, state="dead_letter", next_attempt_at=None),
        lambda value: replace(
            value,
            next_attempt_at=cast(datetime, value.next_attempt_at) + timedelta(seconds=1),
        ),
        lambda value: replace(value, observed_at=NOW - timedelta(seconds=1)),
    ],
)
async def test_failure_receipt_binding_and_retry_matrix_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    mutator: FailureMutator,
) -> None:
    clock = _install_clock(monkeypatch)
    runner, port, _runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={
            "operations.commands": _Handler(
                failure=SchedulerRetryableError("command_poll_retryable")
            )
        },
    )
    port.failure_mutator = mutator

    with pytest.raises(
        SchedulerMutationOutcomeUnknownError,
        match="scheduler_failure_response_is_invalid",
    ):
        await runner.run_once()

    assert len(port.failure_calls) == 1
    assert port.completion_calls == []


@pytest.mark.parametrize(
    "transition",
    ["complete", "fail"],
)
@pytest.mark.parametrize(
    "drift",
    [
        "port_release",
        "port_authority",
        "runtime_account",
        "runtime_holder",
        "runtime_release",
        "runtime_authority",
        "outer_generation",
        "outer_expiry_shrunk",
    ],
)
async def test_post_settlement_drift_never_dispatches_opposite_transition(
    monkeypatch: pytest.MonkeyPatch,
    transition: Literal["complete", "fail"],
    drift: str,
) -> None:
    clock = _install_clock(monkeypatch)
    handler = (
        _Handler(result=OperationCommandRunResult(0, 0))
        if transition == "complete"
        else _Handler(failure=SchedulerRetryableError("command_poll_retryable"))
    )
    runner, port, runtime = _runner(
        monkeypatch,
        clock=clock,
        claim=_claim(),
        handlers={"operations.commands": handler},
    )

    def apply_drift(_rpc: Literal["complete", "fail"]) -> None:
        if drift == "port_release":
            port.release_sha = OTHER_RELEASE_SHA
        elif drift == "port_authority":
            port.persistence_authority = OTHER_PERSISTENCE_AUTHORITY
        elif drift == "runtime_account":
            runtime.account_id = "paper-other"
        elif drift == "runtime_holder":
            runtime.holder_id = "99999999-9999-4999-8999-999999999999"
        elif drift == "runtime_release":
            runtime.release_sha = OTHER_RELEASE_SHA
        elif drift == "runtime_authority":
            runtime.persistence_authority = OTHER_PERSISTENCE_AUTHORITY
        elif drift == "outer_generation":
            runtime.outer_lease = replace(runtime.outer_lease, fencing_token=8)
        else:
            runtime.outer_lease = replace(
                runtime.outer_lease,
                expires_at=NOW + timedelta(microseconds=500_000),
            )

    port.after_rpc = apply_drift

    with pytest.raises(_FailStopTriggered) as captured:
        await runner.run_once()

    expected_reason = (
        "scheduler_post_settlement_outer_lease_changed"
        if drift in {"outer_generation", "outer_expiry_shrunk"}
        else "scheduler_post_settlement_runtime_changed"
    )
    assert captured.value.reason == expected_reason
    assert len(port.completion_calls) == (1 if transition == "complete" else 0)
    assert len(port.failure_calls) == (1 if transition == "fail" else 0)


async def test_convergence_scans_fixed_order_and_dispatches_only_sealed_safe_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_clock(monkeypatch)
    definitions = _definitions()
    command_claim = _claim(enabled=False)
    receipts: dict[
        SchedulerJobKey,
        SchedulerDefinitionConvergenceReceiptV1,
    ] = {
        "operations.commands": _convergence_receipt(
            command_claim.definition,
            status="claimed",
            claim=command_claim,
        ),
        "operations.reconciliation": _convergence_receipt(
            _definition("operations.reconciliation"),
            status="wait",
        ),
        "operations.outbox": _convergence_receipt(
            _definition("operations.outbox"),
            status="manual_resolution",
        ),
    }
    handler = _Handler(result=OperationCommandRunResult(0, 0))
    port = _SchedulerPortStub(None, convergence_by_key=receipts)
    runtime = _RuntimeStub(port)
    convergence = ConvergeDurableSchedulerDefinitions(
        cast(Any, runtime),
        definitions,
        _bindings({"operations.commands": handler}),
        fail_stop=_raise_fail_stop,
    )

    result = await convergence.run_step()

    assert isinstance(result, DurableSchedulerConvergenceResult)
    assert result.outcome == "manual_resolution"
    assert tuple(port.convergence_calls) == SCHEDULER_CONVERGENCE_ORDER
    assert port.claim_calls == 0
    assert handler.calls == 1
    assert handler.bindings[0].run_id == command_claim.run.run_id
    assert len(result.run_results) == 1
    assert len(port.completion_calls) == 1
    assert port.failure_calls == []


async def test_convergence_quiescent_result_has_no_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_clock(monkeypatch)
    port = _SchedulerPortStub(None)
    runtime = _RuntimeStub(port)
    convergence = ConvergeDurableSchedulerDefinitions(
        cast(Any, runtime),
        _definitions(),
        _bindings(),
        fail_stop=_raise_fail_stop,
    )

    result = await convergence.run_step()

    assert result.outcome == "converged"
    assert tuple(port.convergence_calls) == SCHEDULER_CONVERGENCE_ORDER
    assert result.run_results == ()
    assert port.completion_calls == []
    assert port.failure_calls == []


@pytest.mark.parametrize(
    ("job_key", "max_attempts", "max_manual_replays"),
    [
        ("operations.commands", 4, 1),
        ("operations.reconciliation", 3, 2),
        ("operations.outbox", 4, 1),
        ("operations.execution", 2, 0),
        ("operations.settlement", 1, 1),
    ],
)
def test_convergence_rejects_unsafe_fixed_definition_budgets(
    job_key: SchedulerJobKey,
    max_attempts: int,
    max_manual_replays: int,
) -> None:
    port = _SchedulerPortStub(None)
    runtime = _RuntimeStub(port)
    definitions = {definition.job_key: definition for definition in _definitions()}
    definitions[job_key] = _definition(
        job_key,
        max_attempts=max_attempts,
        max_manual_replays=max_manual_replays,
    )

    with pytest.raises(SchedulerInvariantError, match="definition_budget_is_unsafe"):
        ConvergeDurableSchedulerDefinitions(
            cast(Any, runtime),
            tuple(definitions.values()),
            _bindings(),
            fail_stop=_raise_fail_stop,
        )

    assert port.convergence_calls == []


def test_scheduler_core_ast_enforces_capability_and_concurrency_boundary() -> None:
    source_path = Path(scheduler_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    forbidden_identifiers = {
        "DurableSchedulerPort",
        "lease_provider",
        "monotonic_clock",
        "wait_until",
        "_claim_scheduler_invocation_with_clock",
        "_converge_scheduler_definition_invocation_with_clock",
    }
    used_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert forbidden_identifiers.isdisjoint(used_names | imported_names)

    forbidden_modules = {"asyncio", "threading", "concurrent.futures", "time"}
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert forbidden_modules.isdisjoint(imported_modules)

    forbidden_calls = {
        "claim_due_job",
        "converge_job_definition",
        "complete_job_run",
        "fail_job_run",
        "create_task",
        "ensure_future",
        "gather",
        "wait",
        "wait_for",
        "shield",
        "to_thread",
        "run_in_executor",
    }
    attribute_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert forbidden_calls.isdisjoint(attribute_calls)

    forbidden_private_attributes = {
        "_scheduler_port",
        "_claim",
        "_outer_lease",
        "_rpc_start",
        "_settlement_state",
    }
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert forbidden_private_attributes.isdisjoint(attributes)

    forbidden_handlers = {
        "BaseException",
        "CancelledError",
        "SchedulerInvocationPermitRevoked",
        "SchedulerInvocationFailStopReturned",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        assert node.type is not None
        caught = {
            item.id
            for item in ast.walk(node.type)
            if isinstance(item, ast.Name)
        } | {
            item.attr
            for item in ast.walk(node.type)
            if isinstance(item, ast.Attribute)
        }
        assert forbidden_handlers.isdisjoint(caught)

    required_public_calls = {
        "claim_scheduler_invocation",
        "converge_scheduler_definition_invocation",
        "run_with_scheduler_deadline",
        "begin_scheduler_invocation_settlement",
        "complete_scheduler_invocation_settlement",
        "fail_scheduler_invocation_settlement",
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert required_public_calls <= called_names

    fail_stop_calls = required_public_calls - {
        "claim_scheduler_invocation",
        "converge_scheduler_definition_invocation",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id in fail_stop_calls:
            assert "fail_stop" in {keyword.arg for keyword in node.keywords}

    source = source_path.read_text(encoding="utf-8")
    assert "result.invocation" in source


def _install_clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    clock = _Clock(100.0)
    monkeypatch.setattr(deadline_module, "monotonic", clock)
    return clock


def _runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: Any,
    claim: ScheduledJobClaimV1 | None,
    handlers: Mapping[SchedulerJobKey, _Handler] | None = None,
) -> tuple[RunDurableSchedulerOnce, _SchedulerPortStub, _RuntimeStub]:
    monkeypatch.setattr(deadline_module, "monotonic", clock)
    port = _SchedulerPortStub(claim)
    runtime = _RuntimeStub(port)
    runner = RunDurableSchedulerOnce(
        cast(Any, runtime),
        _bindings(handlers),
        fail_stop=cast(FailStop, _raise_fail_stop),
    )
    return runner, port, runtime


def _bindings(
    overrides: Mapping[SchedulerJobKey, _Handler] | None = None,
) -> dict[SchedulerJobKey, SchedulerJobBinding]:
    handlers = dict(overrides or {})
    result: dict[SchedulerJobKey, SchedulerJobBinding] = {}
    for raw_key in sorted(SCHEDULER_JOB_KEYS):
        job_key = cast(SchedulerJobKey, raw_key)
        handler: SchedulerJobHandler = handlers.get(
            job_key,
            _Handler(result=_stage_result(job_key)),
        )
        result[job_key] = SchedulerJobBinding(
            job_key=job_key,
            handler=handler,
            result_validator=SCHEDULER_RESULT_VALIDATORS[job_key],
        )
    return result


def _stage_result(job_key: SchedulerJobKey) -> object:
    if job_key == "operations.commands":
        return OperationCommandRunResult(0, 0)
    if job_key == "operations.execution":
        return ExecutionSupervisorV2RunResult(0, 0, 0, 0, 0, 0, 0)
    if job_key == "operations.settlement":
        return CashSettlementRunResult(0, 0, 0, 0, 0, 0)
    if job_key == "operations.reconciliation":
        return ExecutionReconciliationRunResult(0, 0, 0, 0)
    if job_key == "operations.outbox":
        return AlertOutboxDispatchResult(0, 0, 0)
    raise AssertionError("unknown_scheduler_job")


def _passthrough_result(value: object) -> object:
    return value


def _definitions() -> tuple[ScheduledJobDefinitionV1, ...]:
    return tuple(_definition(key) for key in SCHEDULER_CONVERGENCE_ORDER)


def _definition(
    job_key: SchedulerJobKey = "operations.commands",
    *,
    enabled: bool = True,
    max_attempts: int | None = None,
    max_manual_replays: int | None = None,
) -> ScheduledJobDefinitionV1:
    effectful = job_key in SCHEDULER_EFFECTFUL_JOB_KEYS
    return ScheduledJobDefinitionV1(
        job_key=job_key,
        interval_seconds=2,
        lease_ttl_seconds=30,
        max_attempts=(
            max_attempts if max_attempts is not None else (1 if effectful else 3)
        ),
        retry_base_seconds=2,
        retry_max_seconds=8,
        max_manual_replays=(
            max_manual_replays
            if max_manual_replays is not None
            else (0 if effectful else 1)
        ),
        enabled=enabled,
    )


def _claim(
    *,
    job_key: SchedulerJobKey = "operations.commands",
    enabled: bool = True,
    max_attempts: int | None = None,
    max_manual_replays: int | None = None,
    attempt_count: int = 1,
    lease_seconds: int = 30,
) -> ScheduledJobClaimV1:
    definition = _definition(
        job_key,
        enabled=enabled,
        max_attempts=max_attempts,
        max_manual_replays=max_manual_replays,
    )
    run = ScheduledJobRunV1(
        run_id=RUN_IDS[job_key],
        account_id=ACCOUNT_ID,
        job_key=job_key,
        definition_sha256=definition.definition_sha256,
        state="leased",
        revision=2,
        attempt_count=attempt_count,
        replay_generation=0,
        replay_of_run_id=None,
        scheduled_for=NOW - timedelta(seconds=2),
        available_at=NOW - timedelta(seconds=2),
        created_at=NOW - timedelta(seconds=2),
        updated_at=NOW,
    )
    lease = ScheduledJobLeaseV1(
        lease_token=LEASE_TOKENS[job_key],
        run_id=run.run_id,
        account_id=ACCOUNT_ID,
        holder_id=HOLDER_ID,
        release_sha=RELEASE_SHA,
        outer_fencing_token=7,
        attempt_number=attempt_count,
        run_revision=run.revision,
        leased_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=lease_seconds),
    )
    return ScheduledJobClaimV1(
        definition=definition,
        run=run,
        lease=lease,
        observed_at=NOW,
    )


def _outer_lease() -> WorkerLease:
    return WorkerLease(
        account_id=ACCOUNT_ID,
        holder_id=HOLDER_ID,
        fencing_token=7,
        acquired_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=40),
    )


def _convergence_receipt(
    definition: ScheduledJobDefinitionV1,
    *,
    status: Literal[
        "converged",
        "claimed",
        "wait",
        "manual_resolution",
    ] = "converged",
    claim: ScheduledJobClaimV1 | None = None,
) -> SchedulerDefinitionConvergenceReceiptV1:
    if status == "claimed":
        assert claim is not None
        receipt_definition = claim.definition
    else:
        assert claim is None
        receipt_definition = definition
    active_run_id = (
        claim.run.run_id
        if claim is not None
        else (
            RUN_IDS[definition.job_key]
            if status in {"wait", "manual_resolution"}
            else None
        )
    )
    return SchedulerDefinitionConvergenceReceiptV1(
        status=status,
        definition=ScheduledJobConvergenceDefinitionV1(
            definition_id=DEFINITION_IDS[definition.job_key],
            account_id=ACCOUNT_ID,
            definition=receipt_definition,
            revision=1,
            next_due_at=NOW + timedelta(seconds=2),
            scheduler_state=(
                "blocked" if status == "manual_resolution" else "ready"
            ),
        ),
        claim=claim,
        active_run_id=active_run_id,
        next_eligible_at=(
            NOW + timedelta(seconds=3) if status == "wait" else None
        ),
        reason_code=(
            "active_run_wait"
            if status == "wait"
            else (
                "manual_resolution_required"
                if status == "manual_resolution"
                else None
            )
        ),
        observed_at=NOW,
    )

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Never, Protocol, SupportsIndex
from uuid import UUID

from app.application.ports.durable_scheduler_port import (
    SchedulerMutationOutcomeUnknownError,
    canonical_scheduler_outer_lease,
)
from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    is_persistence_authority,
)
from app.application.services.scheduler_invocation_deadline import (
    SCHEDULER_DEADLINE_FAILURES,
    FailStop,
    SchedulerClaimedInvocation,
    SchedulerConvergenceClaimResult,
    SchedulerInvocationBinding,
    SchedulerInvocationDeadlineExceeded,
    SchedulerInvocationFailStopReturned,
    SchedulerInvocationPermit,
    _SchedulerDispatchRegistrySeal,
    begin_scheduler_invocation_settlement,
    claim_scheduler_invocation,
    complete_scheduler_invocation_settlement,
    converge_scheduler_definition_invocation,
    fail_scheduler_invocation_settlement,
    run_with_scheduler_deadline,
)
from app.application.use_cases.apply_operation_commands import OperationCommandRunResult
from app.application.use_cases.dispatch_alert_outbox import AlertOutboxDispatchResult
from app.application.use_cases.mature_cash_settlements import CashSettlementRunResult
from app.application.use_cases.reconcile_execution_v2 import ExecutionReconciliationRunResult
from app.application.use_cases.run_execution_supervisor_v2 import (
    ExecutionSupervisorV2RunResult,
)
from app.application.use_cases.scheduler_runtime_capability import (
    SchedulerRuntimeCapability,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    SCHEDULER_EFFECTFUL_JOB_KEYS,
    SCHEDULER_JOB_KEYS,
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerJobKey,
    canonical_scheduler_claim,
    canonical_scheduler_convergence_receipt,
    canonical_scheduler_definition,
    scheduler_definition_budget_is_safe,
    scheduler_result_sha256,
    scheduler_retry_delay,
)
from app.domain.scheduler.models import (
    SCHEDULER_RETRYABLE_REASONS as DOMAIN_SCHEDULER_RETRYABLE_REASONS,
)

__all__ = (
    "ConvergeDurableSchedulerDefinitions",
    "DurableSchedulerConvergenceResult",
    "DurableSchedulerRunResult",
    "RunDurableSchedulerOnce",
    "SCHEDULER_CONVERGENCE_ORDER",
    "SCHEDULER_RESULT_VALIDATORS",
    "SCHEDULER_RETRYABLE_REASONS",
    "SchedulerJobBinding",
    "SchedulerJobHandler",
    "SchedulerResultValidator",
    "SchedulerRetryableError",
    "SchedulerRunFailedError",
    "validate_operation_commands_scheduler_result",
    "validate_operation_execution_scheduler_result",
    "validate_operation_outbox_scheduler_result",
    "validate_operation_reconciliation_scheduler_result",
    "validate_operation_settlement_scheduler_result",
)

_ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_RELEASE_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

SCHEDULER_CONVERGENCE_ORDER: tuple[SchedulerJobKey, ...] = (
    "operations.commands",
    "operations.reconciliation",
    "operations.outbox",
    "operations.settlement",
    "operations.execution",
)
SCHEDULER_RETRYABLE_REASONS: Mapping[SchedulerJobKey, frozenset[str]] = (
    DOMAIN_SCHEDULER_RETRYABLE_REASONS
)


class SchedulerJobHandler(Protocol):
    async def __call__(
        self,
        permit: SchedulerInvocationPermit,
        invocation_binding: SchedulerInvocationBinding,
    ) -> object: ...


class SchedulerResultValidator(Protocol):
    def __call__(self, value: object) -> object: ...


@dataclass(frozen=True, slots=True)
class SchedulerJobBinding:
    """One fixed job handler paired with its non-replaceable result contract."""

    job_key: SchedulerJobKey
    handler: SchedulerJobHandler
    result_validator: SchedulerResultValidator

    def __post_init__(self) -> None:
        if self.job_key not in SCHEDULER_JOB_KEYS:
            raise SchedulerInvariantError("scheduler_binding_job_key_is_invalid")
        if not callable(self.handler) or not callable(self.result_validator):
            raise SchedulerInvariantError("scheduler_binding_callable_is_invalid")


def validate_operation_commands_scheduler_result(value: object) -> object:
    """Accept a command drain only when every claimed command was acknowledged."""

    if type(value) is not OperationCommandRunResult:
        raise SchedulerInvariantError("scheduler_command_result_type_is_invalid")
    result = value
    counters = (
        result.claimed,
        result.applied,
        result.failed,
        result.unacknowledged,
    )
    if any(type(counter) is not int or counter < 0 for counter in counters):
        raise SchedulerInvariantError("scheduler_command_result_counter_is_invalid")
    if (
        result.applied + result.failed != result.claimed
        or result.unacknowledged > result.failed
    ):
        raise SchedulerInvariantError("scheduler_command_result_accounting_is_invalid")
    if result.failed != 0 or result.unacknowledged != 0:
        raise SchedulerInvariantError("scheduler_command_result_is_not_successful")
    return result


def validate_operation_execution_scheduler_result(value: object) -> object:
    if type(value) is not ExecutionSupervisorV2RunResult:
        raise SchedulerInvariantError("scheduler_execution_result_type_is_invalid")
    result = value
    counters = (
        result.claimed,
        result.new_candidates,
        result.resumed,
        result.completed,
        result.rescheduled,
        result.blocked,
        result.manual,
        result.failed,
    )
    _require_nonnegative_result_counters(counters, "execution")
    execution_inputs = result.new_candidates + result.resumed
    if (
        result.completed + result.rescheduled > execution_inputs
        or execution_inputs > result.claimed - result.blocked
        or result.completed
        + result.rescheduled
        + result.blocked
        + result.manual
        + result.failed
        != result.claimed
    ):
        raise SchedulerInvariantError("scheduler_execution_result_accounting_is_invalid")
    return result


def validate_operation_settlement_scheduler_result(value: object) -> object:
    if type(value) is not CashSettlementRunResult:
        raise SchedulerInvariantError("scheduler_settlement_result_type_is_invalid")
    result = value
    counters = (
        result.claimed,
        result.completed,
        result.replayed,
        result.retried,
        result.dead_lettered,
        result.failed,
    )
    _require_nonnegative_result_counters(counters, "settlement")
    if (
        result.completed + result.failed != result.claimed
        or result.retried + result.dead_lettered != result.failed
        or result.replayed > result.claimed
    ):
        raise SchedulerInvariantError("scheduler_settlement_result_accounting_is_invalid")
    return result


def validate_operation_reconciliation_scheduler_result(value: object) -> object:
    if type(value) is not ExecutionReconciliationRunResult:
        raise SchedulerInvariantError("scheduler_reconciliation_result_type_is_invalid")
    result = value
    counters = (
        result.claimed,
        result.completed,
        result.rescheduled,
        result.manual,
        result.failed,
        result.unknown_listed,
        result.unknown_claimed,
        result.unknown_resumed,
        result.unknown_applied,
        result.unknown_replayed,
        result.unknown_failed,
    )
    _require_nonnegative_result_counters(counters, "reconciliation")
    generic_claimed = result.claimed - result.unknown_claimed - result.unknown_resumed
    generic_completed = result.completed - result.unknown_applied
    generic_failed = result.failed - result.unknown_failed
    if (
        result.unknown_claimed + result.unknown_resumed > result.unknown_listed
        or result.unknown_applied > result.unknown_claimed + result.unknown_resumed
        or result.unknown_applied + result.unknown_failed != result.unknown_listed
        or result.unknown_replayed > result.unknown_applied
        or min(generic_claimed, generic_completed, generic_failed) < 0
        or generic_completed
        + result.rescheduled
        + result.manual
        + generic_failed
        != generic_claimed
    ):
        raise SchedulerInvariantError(
            "scheduler_reconciliation_result_accounting_is_invalid"
        )
    return result


def validate_operation_outbox_scheduler_result(value: object) -> object:
    if type(value) is not AlertOutboxDispatchResult:
        raise SchedulerInvariantError("scheduler_outbox_result_type_is_invalid")
    result = value
    counters = (result.claimed, result.delivered, result.failed)
    _require_nonnegative_result_counters(counters, "outbox")
    if result.delivered + result.failed != result.claimed:
        raise SchedulerInvariantError("scheduler_outbox_result_accounting_is_invalid")
    return result


def _require_nonnegative_result_counters(
    counters: tuple[int, ...],
    stage: str,
) -> None:
    if any(type(counter) is not int or counter < 0 for counter in counters):
        raise SchedulerInvariantError(f"scheduler_{stage}_result_counter_is_invalid")


SCHEDULER_RESULT_VALIDATORS: Mapping[SchedulerJobKey, SchedulerResultValidator] = (
    MappingProxyType(
        {
            "operations.commands": validate_operation_commands_scheduler_result,
            "operations.execution": validate_operation_execution_scheduler_result,
            "operations.settlement": validate_operation_settlement_scheduler_result,
            "operations.reconciliation": validate_operation_reconciliation_scheduler_result,
            "operations.outbox": validate_operation_outbox_scheduler_result,
        }
    )
)


class SchedulerRetryableError(KnownFailClosedError):
    """The only handler exception allowed to request an automatic retry."""

    def __init__(self, reason_code: str) -> None:
        _require_reason(reason_code, "retryable_reason_code")
        self.reason_code = reason_code
        super().__init__("durable_scheduler", reason_code)


class SchedulerRunFailedError(KnownFailClosedError):
    def __init__(
        self,
        safe_message: str,
        *,
        run_id: str,
        reason_code: str,
        failure_receipt: ScheduledJobFailureReceiptV1,
    ) -> None:
        self.run_id = run_id
        self.reason_code = reason_code
        self.failure_receipt = failure_receipt
        super().__init__("durable_scheduler", safe_message)


@dataclass(frozen=True, slots=True)
class DurableSchedulerRunResult:
    outcome: Literal["idle", "succeeded", "retry_wait", "dead_letter"]
    run_id: str | None
    job_key: SchedulerJobKey | None
    result_sha256: str | None
    completion: ScheduledJobCompletionReceiptV1 | None
    failure: ScheduledJobFailureReceiptV1 | None
    handler_result: object | None

    def __post_init__(self) -> None:
        if self.outcome == "idle":
            if any(
                value is not None
                for value in (
                    self.run_id,
                    self.job_key,
                    self.result_sha256,
                    self.completion,
                    self.failure,
                    self.handler_result,
                )
            ):
                raise SchedulerInvariantError("idle_scheduler_result_has_run_data")
            return
        _require_uuid(self.run_id, "run_result_run_id")
        if self.job_key not in SCHEDULER_JOB_KEYS:
            raise SchedulerInvariantError("run_result_job_key_is_invalid")
        _require_sha256(self.result_sha256, "run_result_sha256")
        if self.outcome == "succeeded":
            if self.completion is None or self.failure is not None:
                raise SchedulerInvariantError("successful_scheduler_result_is_invalid")
        elif self.outcome in {"retry_wait", "dead_letter"}:
            if (
                self.failure is None
                or self.completion is not None
                or self.handler_result is not None
                or self.failure.state != self.outcome
            ):
                raise SchedulerInvariantError("failed_scheduler_result_is_invalid")
        else:
            raise SchedulerInvariantError("scheduler_result_outcome_is_invalid")


@dataclass(frozen=True, slots=True)
class DurableSchedulerConvergenceResult:
    outcome: Literal["converged", "claimed", "wait", "manual_resolution"]
    receipts: tuple[SchedulerDefinitionConvergenceReceiptV1, ...]
    run_results: tuple[DurableSchedulerRunResult, ...]

    def __post_init__(self) -> None:
        if (
            tuple(receipt.definition.job_key for receipt in self.receipts)
            != SCHEDULER_CONVERGENCE_ORDER
            or self.outcome != _convergence_outcome(self.receipts)
        ):
            raise SchedulerInvariantError("scheduler_convergence_result_is_invalid")
        claimed = tuple(receipt for receipt in self.receipts if receipt.status == "claimed")
        if len(claimed) != len(self.run_results):
            raise SchedulerInvariantError("scheduler_convergence_result_is_invalid")
        for receipt, run_result in zip(claimed, self.run_results, strict=True):
            if (
                receipt.claim is None
                or run_result.run_id != receipt.claim.run.run_id
                or run_result.job_key != receipt.claim.job_key
                or receipt.claim.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS
            ):
                raise SchedulerInvariantError("scheduler_convergence_result_is_invalid")

    @property
    def run_result(self) -> DurableSchedulerRunResult | None:
        return self.run_results[0] if len(self.run_results) == 1 else None


@dataclass(frozen=True, slots=True)
class _RuntimePin:
    scheduler_port: object
    account_id: str
    holder_id: str
    release_sha: str
    persistence_authority: PersistenceAuthority


@dataclass(frozen=True, slots=True)
class _FailurePlan:
    reason_code: str
    retryable: bool
    terminal_message: str | None


class _HandlerRaised(Exception):
    """Hide handler-owned Exception types from supervisor control flow."""


class _EffectAuthorizedSchedulerRegistryIssuance:
    __slots__ = ()


_EFFECT_AUTHORIZED_SCHEDULER_REGISTRY_ISSUANCE = (
    _EffectAuthorizedSchedulerRegistryIssuance()
)


class _SchedulerInvocationDispatcher:
    def __init__(
        self,
        runtime: SchedulerRuntimeCapability,
        bindings: Mapping[SchedulerJobKey, SchedulerJobBinding],
        *,
        fail_stop: FailStop,
        dispatch_registry_seal: _SchedulerDispatchRegistrySeal | None = None,
    ) -> None:
        if not callable(fail_stop):
            raise SchedulerInvariantError("scheduler_fail_stop_is_invalid")
        self.runtime = runtime
        self.fail_stop = fail_stop
        self.runtime_pin, _outer_lease = _capture_runtime(runtime)
        self.bindings = _fixed_scheduler_bindings(bindings)
        self.dispatch_registry_seal = dispatch_registry_seal
        self._require_registry_intact(fail_stop_on_error=False)

    def _require_registry_intact(self, *, fail_stop_on_error: bool) -> None:
        seal = self.dispatch_registry_seal
        if seal is None:
            return
        try:
            if type(seal) is not _SchedulerDispatchRegistrySeal:
                raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_invalid")
            for job_key, binding in self.bindings.items():
                seal.assert_authorized(
                    self.runtime,
                    job_key=job_key,
                    handler=binding.handler,
                    validator=binding.result_validator,
                )
        except SchedulerInvariantError:
            if fail_stop_on_error:
                _invoke_fail_stop(self.fail_stop, "scheduler_dispatch_registry_changed")
            raise

    def _effect_runtime_for(
        self,
        binding: SchedulerJobBinding,
    ) -> SchedulerRuntimeCapability | None:
        seal = self.dispatch_registry_seal
        if seal is None:
            return None
        try:
            seal.assert_authorized(
                self.runtime,
                job_key=binding.job_key,
                handler=binding.handler,
                validator=binding.result_validator,
            )
        except SchedulerInvariantError:
            _invoke_fail_stop(self.fail_stop, "scheduler_dispatch_registry_changed")
        return self.runtime

    async def dispatch(
        self,
        invocation: SchedulerClaimedInvocation,
        *,
        allow_disabled_definition: bool,
    ) -> DurableSchedulerRunResult:
        self._require_registry_intact(fail_stop_on_error=True)
        claim, binding = _validate_invocation_policy(
            invocation,
            self.runtime_pin,
            self.bindings,
            allow_disabled_definition=allow_disabled_definition,
        )
        current_outer_lease = _require_runtime_pin_or_fail_stop(
            self.runtime,
            self.runtime_pin,
            fail_stop=self.fail_stop,
            reason="scheduler_dispatch_runtime_changed",
        )
        _require_same_outer_generation_or_fail_stop(
            invocation.outer_lease,
            current_outer_lease,
            fail_stop=self.fail_stop,
            reason="scheduler_dispatch_outer_lease_changed",
        )

        async def invoke_handler(permit: SchedulerInvocationPermit) -> object:
            self._require_registry_intact(fail_stop_on_error=True)
            try:
                result = await binding.handler(permit, invocation.binding)
            except SchedulerRetryableError as exc:
                if type(exc) is SchedulerRetryableError:
                    raise
                raise _HandlerRaised from None
            except Exception:
                raise _HandlerRaised from None
            self._require_registry_intact(fail_stop_on_error=True)
            return result

        try:
            raw_handler_result = await run_with_scheduler_deadline(
                invoke_handler,
                invocation=invocation,
                fail_stop=self.fail_stop,
                effect_issuer=binding.handler,
                effect_runtime=self._effect_runtime_for(binding),
                dispatch_registry_seal=self.dispatch_registry_seal,
            )
        except SchedulerInvocationDeadlineExceeded as exc:
            failure_plan = _deadline_failure_plan(
                exc,
                job_key=claim.job_key,
                fail_stop=self.fail_stop,
            )
        except SchedulerRetryableError as exc:
            if type(exc) is not SchedulerRetryableError:
                _invoke_fail_stop(
                    self.fail_stop,
                    "scheduler_retryable_exception_provenance_invalid",
                )
            failure_plan = _handler_retry_failure_plan(exc, claim.job_key)
        except _HandlerRaised:
            failure_plan = _FailurePlan(
                reason_code="scheduler_handler_unknown_failure",
                retryable=False,
                terminal_message="scheduler_handler_failed_closed",
            )
        else:
            self._require_registry_intact(fail_stop_on_error=True)
            try:
                handler_result = binding.result_validator(raw_handler_result)
                result_sha256 = scheduler_result_sha256(handler_result)
            except Exception:
                failure_plan = _FailurePlan(
                    reason_code="scheduler_handler_result_rejected",
                    retryable=False,
                    terminal_message="scheduler_handler_result_failed_closed",
                )
            else:
                self._require_registry_intact(fail_stop_on_error=True)
                return await self._complete(
                    invocation,
                    claim,
                    handler_result=handler_result,
                    result_sha256=result_sha256,
                )

        return await self._fail(invocation, claim, failure_plan)

    async def _complete(
        self,
        invocation: SchedulerClaimedInvocation,
        claim: ScheduledJobClaimV1,
        *,
        handler_result: object,
        result_sha256: str,
    ) -> DurableSchedulerRunResult:
        self._require_registry_intact(fail_stop_on_error=True)
        authorization = begin_scheduler_invocation_settlement(
            self.runtime,
            invocation,
            fail_stop=self.fail_stop,
        )
        raw_receipt = await complete_scheduler_invocation_settlement(
            authorization,
            result_sha256=result_sha256,
            fail_stop=self.fail_stop,
        )
        outer_lease = _assert_post_settlement_runtime(
            self.runtime,
            self.runtime_pin,
            invocation,
            fail_stop=self.fail_stop,
        )
        receipt = _canonical_completion_response(raw_receipt)
        _validate_completion_receipt_identity(receipt, claim, result_sha256)
        _validate_settlement_receipt_time(receipt.observed_at, claim, outer_lease)
        return DurableSchedulerRunResult(
            outcome="succeeded",
            run_id=claim.run.run_id,
            job_key=claim.job_key,
            result_sha256=result_sha256,
            completion=receipt,
            failure=None,
            handler_result=handler_result,
        )

    async def _fail(
        self,
        invocation: SchedulerClaimedInvocation,
        claim: ScheduledJobClaimV1,
        plan: _FailurePlan,
    ) -> DurableSchedulerRunResult:
        self._require_registry_intact(fail_stop_on_error=True)
        failure_sha256 = scheduler_result_sha256(
            {
                "classification": "retryable" if plan.retryable else "terminal",
                "job_key": claim.job_key,
                "reason_code": plan.reason_code,
            }
        )
        authorization = begin_scheduler_invocation_settlement(
            self.runtime,
            invocation,
            fail_stop=self.fail_stop,
        )
        raw_receipt = await fail_scheduler_invocation_settlement(
            authorization,
            failure_reason_code=plan.reason_code,
            failure_sha256=failure_sha256,
            retryable=plan.retryable,
            fail_stop=self.fail_stop,
        )
        outer_lease = _assert_post_settlement_runtime(
            self.runtime,
            self.runtime_pin,
            invocation,
            fail_stop=self.fail_stop,
        )
        receipt = _canonical_failure_response(raw_receipt)
        _validate_failure_receipt_identity(
            receipt,
            claim,
            reason_code=plan.reason_code,
            failure_sha256=failure_sha256,
            retryable=plan.retryable,
        )
        _validate_settlement_receipt_time(receipt.observed_at, claim, outer_lease)
        if not plan.retryable:
            if plan.terminal_message is None:
                raise SchedulerInvariantError("scheduler_terminal_failure_plan_is_invalid")
            raise SchedulerRunFailedError(
                plan.terminal_message,
                run_id=claim.run.run_id,
                reason_code=plan.reason_code,
                failure_receipt=receipt,
            )
        return DurableSchedulerRunResult(
            outcome=receipt.state,
            run_id=claim.run.run_id,
            job_key=claim.job_key,
            result_sha256=failure_sha256,
            completion=None,
            failure=receipt,
            handler_result=None,
        )


class RunDurableSchedulerOnce:
    """Claim one due run through an attested runtime and settle it once."""

    def __init__(
        self,
        runtime: SchedulerRuntimeCapability,
        bindings: Mapping[SchedulerJobKey, SchedulerJobBinding],
        *,
        fail_stop: FailStop,
    ) -> None:
        self._dispatcher = _SchedulerInvocationDispatcher(
            runtime,
            bindings,
            fail_stop=fail_stop,
        )

    @classmethod
    def _effect_authorized(
        cls,
        registry: _EffectAuthorizedSchedulerRegistry,
        *,
        fail_stop: FailStop,
    ) -> RunDurableSchedulerOnce:
        registry._assert_intact()
        instance = cls.__new__(cls)
        instance._dispatcher = _SchedulerInvocationDispatcher(
            registry._runtime,
            registry._bindings,
            fail_stop=fail_stop,
            dispatch_registry_seal=registry._seal,
        )
        return instance

    async def run_once(self) -> DurableSchedulerRunResult:
        self._dispatcher._require_registry_intact(fail_stop_on_error=True)
        _require_runtime_pin(self._dispatcher.runtime, self._dispatcher.runtime_pin)
        invocation = await claim_scheduler_invocation(self._dispatcher.runtime)
        _require_runtime_pin_or_fail_stop(
            self._dispatcher.runtime,
            self._dispatcher.runtime_pin,
            fail_stop=self._dispatcher.fail_stop,
            reason="scheduler_claim_runtime_changed",
        )
        if invocation is None:
            return DurableSchedulerRunResult(
                outcome="idle",
                run_id=None,
                job_key=None,
                result_sha256=None,
                completion=None,
                failure=None,
                handler_result=None,
            )
        return await self._dispatcher.dispatch(
            invocation,
            allow_disabled_definition=False,
        )


class ConvergeDurableSchedulerDefinitions:
    """Converge all definitions and drain only sealed safe recovery claims."""

    def __init__(
        self,
        runtime: SchedulerRuntimeCapability,
        definitions: tuple[ScheduledJobDefinitionV1, ...],
        bindings: Mapping[SchedulerJobKey, SchedulerJobBinding],
        *,
        fail_stop: FailStop,
    ) -> None:
        self._definitions = _fixed_scheduler_definitions(definitions)
        self._dispatcher = _SchedulerInvocationDispatcher(
            runtime,
            bindings,
            fail_stop=fail_stop,
        )

    @classmethod
    def _effect_authorized(
        cls,
        registry: _EffectAuthorizedSchedulerRegistry,
        definitions: tuple[ScheduledJobDefinitionV1, ...],
        *,
        fail_stop: FailStop,
    ) -> ConvergeDurableSchedulerDefinitions:
        registry._assert_intact()
        instance = cls.__new__(cls)
        instance._definitions = _fixed_scheduler_definitions(definitions)
        instance._dispatcher = _SchedulerInvocationDispatcher(
            registry._runtime,
            registry._bindings,
            fail_stop=fail_stop,
            dispatch_registry_seal=registry._seal,
        )
        return instance

    async def run_step(self) -> DurableSchedulerConvergenceResult:
        self._dispatcher._require_registry_intact(fail_stop_on_error=True)
        receipts: list[SchedulerDefinitionConvergenceReceiptV1] = []
        run_results: list[DurableSchedulerRunResult] = []
        for definition in self._definitions:
            _require_runtime_pin(self._dispatcher.runtime, self._dispatcher.runtime_pin)
            result = await converge_scheduler_definition_invocation(
                self._dispatcher.runtime,
                definition,
            )
            _require_runtime_pin_or_fail_stop(
                self._dispatcher.runtime,
                self._dispatcher.runtime_pin,
                fail_stop=self._dispatcher.fail_stop,
                reason="scheduler_convergence_runtime_changed",
            )
            receipt, invocation = _validate_convergence_result(
                result,
                definition,
                account_id=self._dispatcher.runtime_pin.account_id,
            )
            receipts.append(receipt)
            if invocation is not None:
                run_results.append(
                    await self._dispatcher.dispatch(
                        invocation,
                        allow_disabled_definition=True,
                    )
                )
        canonical_receipts = tuple(receipts)
        return DurableSchedulerConvergenceResult(
            outcome=_convergence_outcome(canonical_receipts),
            receipts=canonical_receipts,
            run_results=tuple(run_results),
        )


def _fixed_scheduler_bindings(
    bindings: Mapping[SchedulerJobKey, SchedulerJobBinding],
) -> Mapping[SchedulerJobKey, SchedulerJobBinding]:
    try:
        copied = dict(bindings)
    except Exception:
        raise SchedulerInvariantError("scheduler_binding_registry_is_invalid") from None
    if set(copied) != SCHEDULER_JOB_KEYS:
        raise SchedulerInvariantError("scheduler_binding_registry_is_incomplete")
    canonical: dict[SchedulerJobKey, SchedulerJobBinding] = {}
    for job_key, binding in copied.items():
        if type(binding) is not SchedulerJobBinding:
            raise SchedulerInvariantError("scheduler_binding_registry_is_invalid")
        reconstructed = SchedulerJobBinding(
            job_key=binding.job_key,
            handler=binding.handler,
            result_validator=binding.result_validator,
        )
        if (
            binding.job_key != job_key
            or binding.result_validator is not SCHEDULER_RESULT_VALIDATORS[job_key]
        ):
            raise SchedulerInvariantError("scheduler_binding_registry_is_invalid")
        canonical[job_key] = reconstructed
    return MappingProxyType(canonical)


class _EffectAuthorizedSchedulerRegistry:
    """Atomic runtime, handler registry, and provenance seal bundle."""

    __slots__ = ("_runtime", "_bindings", "_seal", "_issuance")
    _bindings: Mapping[SchedulerJobKey, SchedulerJobBinding]
    _issuance: _EffectAuthorizedSchedulerRegistryIssuance
    _runtime: SchedulerRuntimeCapability
    _seal: _SchedulerDispatchRegistrySeal

    def __init__(
        self,
        runtime: SchedulerRuntimeCapability,
        bindings: Mapping[SchedulerJobKey, SchedulerJobBinding],
        seal: _SchedulerDispatchRegistrySeal,
        *,
        _issuance: object,
    ) -> None:
        if _issuance is not _EFFECT_AUTHORIZED_SCHEDULER_REGISTRY_ISSUANCE:
            raise SchedulerInvariantError(
                "scheduler_effect_registry_is_not_issued"
            )
        if type(seal) is not _SchedulerDispatchRegistrySeal:
            raise SchedulerInvariantError("scheduler_effect_registry_seal_is_invalid")
        fixed_bindings = _fixed_scheduler_bindings(bindings)
        runtime.assert_intact()
        for job_key, binding in fixed_bindings.items():
            seal.assert_authorized(
                runtime,
                job_key=job_key,
                handler=binding.handler,
                validator=binding.result_validator,
            )
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_bindings", fixed_bindings)
        object.__setattr__(self, "_seal", seal)
        object.__setattr__(
            self,
            "_issuance",
            _EFFECT_AUTHORIZED_SCHEDULER_REGISTRY_ISSUANCE,
        )

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_effect_registry_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_effect_registry_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_effect_registry_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_effect_registry_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_effect_registry_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_effect_registry_is_not_serializable")

    def _assert_intact(self) -> None:
        try:
            if (
                self._issuance
                is not _EFFECT_AUTHORIZED_SCHEDULER_REGISTRY_ISSUANCE
                or type(self._seal) is not _SchedulerDispatchRegistrySeal
            ):
                raise SchedulerInvariantError("scheduler_effect_registry_is_invalid")
            self._runtime.assert_intact()
            fixed_bindings = _fixed_scheduler_bindings(self._bindings)
            if any(
                binding.handler is not self._bindings[job_key].handler
                for job_key, binding in fixed_bindings.items()
            ):
                raise SchedulerInvariantError("scheduler_effect_registry_is_invalid")
            for job_key, binding in self._bindings.items():
                self._seal.assert_authorized(
                    self._runtime,
                    job_key=job_key,
                    handler=binding.handler,
                    validator=binding.result_validator,
                )
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_effect_registry_is_invalid") from None


def _issue_effect_authorized_scheduler_registry(
    runtime: SchedulerRuntimeCapability,
    bindings: Mapping[SchedulerJobKey, SchedulerJobBinding],
    seal: _SchedulerDispatchRegistrySeal,
) -> _EffectAuthorizedSchedulerRegistry:
    """Private factory hook that atomically binds an already attested registry."""

    return _EffectAuthorizedSchedulerRegistry(
        runtime,
        bindings,
        seal,
        _issuance=_EFFECT_AUTHORIZED_SCHEDULER_REGISTRY_ISSUANCE,
    )


def _fixed_scheduler_definitions(
    definitions: tuple[ScheduledJobDefinitionV1, ...],
) -> tuple[ScheduledJobDefinitionV1, ...]:
    if type(definitions) is not tuple:
        raise SchedulerInvariantError("scheduler_definition_registry_is_invalid")
    canonical = tuple(canonical_scheduler_definition(item) for item in definitions)
    if len(canonical) != len(SCHEDULER_JOB_KEYS):
        raise SchedulerInvariantError("scheduler_definition_registry_is_incomplete")
    by_key = {item.job_key: item for item in canonical}
    if set(by_key) != SCHEDULER_JOB_KEYS or len(by_key) != len(canonical):
        raise SchedulerInvariantError("scheduler_definition_registry_is_incomplete")
    for definition in canonical:
        if not scheduler_definition_budget_is_safe(definition):
            reason = (
                "scheduler_effectful_definition_budget_is_unsafe"
                if definition.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS
                else "scheduler_safe_definition_budget_is_unsafe"
            )
            raise SchedulerInvariantError(reason)
    return tuple(by_key[job_key] for job_key in SCHEDULER_CONVERGENCE_ORDER)


def _validate_invocation_policy(
    invocation: SchedulerClaimedInvocation,
    runtime_pin: _RuntimePin,
    bindings: Mapping[SchedulerJobKey, SchedulerJobBinding],
    *,
    allow_disabled_definition: bool,
) -> tuple[ScheduledJobClaimV1, SchedulerJobBinding]:
    if type(invocation) is not SchedulerClaimedInvocation:
        raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_issued")
    claim = canonical_scheduler_claim(invocation.claim)
    invocation_binding = invocation.binding
    if type(invocation_binding) is not SchedulerInvocationBinding:
        raise SchedulerInvariantError("scheduler_invocation_binding_is_invalid")
    if (
        claim != invocation.claim
        or invocation_binding.job_key != claim.job_key
        or invocation_binding.account_id != claim.run.account_id
        or invocation_binding.holder_id != claim.lease.holder_id
        or invocation_binding.release_sha != claim.lease.release_sha
        or invocation_binding.run_id != claim.run.run_id
        or invocation_binding.run_revision != claim.run.revision
        or invocation_binding.lease_token != claim.lease.lease_token
        or invocation_binding.outer_fencing_token != claim.lease.outer_fencing_token
        or invocation_binding.definition_sha256 != claim.definition.definition_sha256
        or invocation_binding.attempt_number != claim.lease.attempt_number
        or invocation.persistence_authority != runtime_pin.persistence_authority
        or claim.run.account_id != runtime_pin.account_id
        or claim.lease.holder_id != runtime_pin.holder_id
        or claim.lease.release_sha != runtime_pin.release_sha
    ):
        raise SchedulerInvariantError("scheduler_claim_fixed_binding_mismatch")
    if not scheduler_definition_budget_is_safe(claim.definition):
        raise SchedulerInvariantError("scheduler_claim_definition_budget_is_unsafe")
    if claim.run.available_at > claim.observed_at:
        raise SchedulerInvariantError("scheduler_claim_is_not_available")
    if allow_disabled_definition:
        if claim.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS:
            raise SchedulerInvariantError("scheduler_convergence_claim_policy_violation")
    elif not claim.definition.enabled:
        raise SchedulerInvariantError("scheduler_normal_claim_definition_is_disabled")
    binding = bindings.get(claim.job_key)
    if binding is None:
        raise SchedulerInvariantError("scheduler_claim_fixed_binding_mismatch")
    return claim, binding


def _deadline_failure_plan(
    error: SchedulerInvocationDeadlineExceeded,
    *,
    job_key: SchedulerJobKey,
    fail_stop: FailStop,
) -> _FailurePlan:
    expected = SCHEDULER_DEADLINE_FAILURES[job_key]
    if (
        type(error) is not SchedulerInvocationDeadlineExceeded
        or error.job_key != job_key
        or error.reason_code != expected.reason_code
        or error.retryable is not expected.retryable
    ):
        _invoke_fail_stop(fail_stop, "scheduler_deadline_failure_provenance_invalid")
    return _FailurePlan(
        reason_code=expected.reason_code,
        retryable=expected.retryable,
        terminal_message=None if expected.retryable else "scheduler_deadline_failed_closed",
    )


def _handler_retry_failure_plan(
    error: SchedulerRetryableError,
    job_key: SchedulerJobKey,
) -> _FailurePlan:
    if error.reason_code in SCHEDULER_RETRYABLE_REASONS[job_key]:
        return _FailurePlan(
            reason_code=error.reason_code,
            retryable=True,
            terminal_message=None,
        )
    return _FailurePlan(
        reason_code="scheduler_retry_classification_not_allowed",
        retryable=False,
        terminal_message="scheduler_retry_classification_failed_closed",
    )


def _capture_runtime(
    runtime: SchedulerRuntimeCapability,
) -> tuple[_RuntimePin, WorkerLease]:
    try:
        runtime.assert_intact()
        scheduler_port = runtime.scheduler_port
        account_id = runtime.account_id
        holder_id = runtime.holder_id
        release_sha = runtime.release_sha
        persistence_authority = runtime.persistence_authority
        outer_lease = canonical_scheduler_outer_lease(runtime.current_outer_lease())
        port_release_sha = scheduler_port.release_sha
        port_authority = scheduler_port.persistence_authority
    except SchedulerInvariantError:
        raise
    except Exception:
        raise SchedulerInvariantError("scheduler_runtime_capability_is_invalid") from None
    if (
        type(account_id) is not str
        or _ACCOUNT_RE.fullmatch(account_id) is None
        or not _is_uuid(holder_id)
        or type(release_sha) is not str
        or _RELEASE_SHA_RE.fullmatch(release_sha) is None
        or not is_persistence_authority(persistence_authority)
        or type(port_release_sha) is not str
        or _RELEASE_SHA_RE.fullmatch(port_release_sha) is None
        or not is_persistence_authority(port_authority)
        or port_release_sha != release_sha
        or port_authority != persistence_authority
        or outer_lease.account_id != account_id
        or outer_lease.holder_id != holder_id
        or any(
            not callable(getattr(scheduler_port, operation, None))
            for operation in (
                "claim_due_job",
                "converge_job_definition",
                "complete_job_run",
                "fail_job_run",
            )
        )
    ):
        raise SchedulerInvariantError("scheduler_runtime_capability_is_invalid")
    return (
        _RuntimePin(
            scheduler_port=scheduler_port,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
            persistence_authority=persistence_authority,
        ),
        outer_lease,
    )


def _require_runtime_pin(
    runtime: SchedulerRuntimeCapability,
    expected: _RuntimePin,
) -> WorkerLease:
    current, outer_lease = _capture_runtime(runtime)
    if not _runtime_pins_match(current, expected):
        raise SchedulerInvariantError("scheduler_runtime_capability_changed")
    return outer_lease


def _require_runtime_pin_or_fail_stop(
    runtime: SchedulerRuntimeCapability,
    expected: _RuntimePin,
    *,
    fail_stop: FailStop,
    reason: str,
) -> WorkerLease:
    try:
        return _require_runtime_pin(runtime, expected)
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, reason)


def _runtime_pins_match(current: _RuntimePin, expected: _RuntimePin) -> bool:
    return (
        current.scheduler_port is expected.scheduler_port
        and current.account_id == expected.account_id
        and current.holder_id == expected.holder_id
        and current.release_sha == expected.release_sha
        and current.persistence_authority == expected.persistence_authority
    )


def _assert_post_settlement_runtime(
    runtime: SchedulerRuntimeCapability,
    runtime_pin: _RuntimePin,
    invocation: SchedulerClaimedInvocation,
    *,
    fail_stop: FailStop,
) -> WorkerLease:
    current_outer_lease = _require_runtime_pin_or_fail_stop(
        runtime,
        runtime_pin,
        fail_stop=fail_stop,
        reason="scheduler_post_settlement_runtime_changed",
    )
    binding = invocation.binding
    if (
        binding.account_id != runtime_pin.account_id
        or binding.holder_id != runtime_pin.holder_id
        or binding.release_sha != runtime_pin.release_sha
        or invocation.persistence_authority != runtime_pin.persistence_authority
    ):
        _invoke_fail_stop(fail_stop, "scheduler_post_settlement_provenance_changed")
    _require_same_outer_generation_or_fail_stop(
        invocation.outer_lease,
        current_outer_lease,
        fail_stop=fail_stop,
        reason="scheduler_post_settlement_outer_lease_changed",
    )
    return current_outer_lease


def _require_same_outer_generation_or_fail_stop(
    captured: WorkerLease,
    current: WorkerLease,
    *,
    fail_stop: FailStop,
    reason: str,
) -> None:
    if (
        current.account_id != captured.account_id
        or current.holder_id != captured.holder_id
        or current.fencing_token != captured.fencing_token
        or current.acquired_at != captured.acquired_at
        or current.expires_at < captured.expires_at
    ):
        _invoke_fail_stop(fail_stop, reason)


def _canonical_completion_response(value: object) -> ScheduledJobCompletionReceiptV1:
    if type(value) is not ScheduledJobCompletionReceiptV1:
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_success_response_is_invalid"
        )
    try:
        return ScheduledJobCompletionReceiptV1(
            run_id=value.run_id,
            run_revision=value.run_revision,
            attempt_count=value.attempt_count,
            next_attempt_at=value.next_attempt_at,
            failure_reason_code=value.failure_reason_code,
            result_sha256=value.result_sha256,
            observed_at=value.observed_at,
            state=value.state,
        )
    except Exception:
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_success_response_is_invalid"
        ) from None


def _canonical_failure_response(value: object) -> ScheduledJobFailureReceiptV1:
    if type(value) is not ScheduledJobFailureReceiptV1:
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_failure_response_is_invalid"
        )
    try:
        return ScheduledJobFailureReceiptV1(
            run_id=value.run_id,
            run_revision=value.run_revision,
            attempt_count=value.attempt_count,
            state=value.state,
            failure_reason_code=value.failure_reason_code,
            result_sha256=value.result_sha256,
            next_attempt_at=value.next_attempt_at,
            observed_at=value.observed_at,
        )
    except Exception:
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_failure_response_is_invalid"
        ) from None


def _validate_completion_receipt_identity(
    receipt: ScheduledJobCompletionReceiptV1,
    claim: ScheduledJobClaimV1,
    result_sha256: str,
) -> None:
    if (
        receipt.run_id != claim.run.run_id
        or receipt.run_revision != claim.run.revision + 1
        or receipt.attempt_count != claim.run.attempt_count
        or receipt.result_sha256 != result_sha256
        or receipt.state != "succeeded"
        or receipt.next_attempt_at is not None
        or receipt.failure_reason_code is not None
        or receipt.observed_at < claim.observed_at
    ):
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_success_response_is_invalid"
        )


def _validate_failure_receipt_identity(
    receipt: ScheduledJobFailureReceiptV1,
    claim: ScheduledJobClaimV1,
    *,
    reason_code: str,
    failure_sha256: str,
    retryable: bool,
) -> None:
    expected_state: Literal["retry_wait", "dead_letter"] = (
        "retry_wait"
        if retryable and claim.run.attempt_count < claim.definition.max_attempts
        else "dead_letter"
    )
    if (
        receipt.run_id != claim.run.run_id
        or receipt.run_revision != claim.run.revision + 1
        or receipt.attempt_count != claim.run.attempt_count
        or receipt.failure_reason_code != reason_code
        or receipt.result_sha256 != failure_sha256
        or receipt.state != expected_state
        or receipt.observed_at < claim.observed_at
    ):
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_failure_response_is_invalid"
        )
    if expected_state == "dead_letter":
        if receipt.next_attempt_at is not None:
            raise SchedulerMutationOutcomeUnknownError(
                "scheduler_failure_response_is_invalid"
            )
        return
    if receipt.next_attempt_at is None:
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_failure_response_is_invalid"
        )
    try:
        transition_at = receipt.next_attempt_at - scheduler_retry_delay(
            claim.definition,
            claim.run.attempt_count,
        )
    except (OverflowError, SchedulerInvariantError):
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_failure_response_is_invalid"
        ) from None
    if not claim.observed_at <= transition_at <= receipt.observed_at:
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_failure_response_is_invalid"
        )


def _validate_settlement_receipt_time(
    observed_at: datetime,
    claim: ScheduledJobClaimV1,
    outer_lease: WorkerLease,
) -> None:
    if observed_at < claim.observed_at or not outer_lease.is_active(observed_at):
        raise SchedulerMutationOutcomeUnknownError(
            "scheduler_settlement_response_time_is_invalid"
        )


def _validate_convergence_result(
    result: SchedulerConvergenceClaimResult,
    desired_definition: ScheduledJobDefinitionV1,
    *,
    account_id: str,
) -> tuple[
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerClaimedInvocation | None,
]:
    if type(result) is not SchedulerConvergenceClaimResult:
        raise SchedulerInvariantError("scheduler_convergence_result_is_not_issued")
    receipt = canonical_scheduler_convergence_receipt(result.receipt)
    invocation = result.invocation
    if (
        receipt.definition.account_id != account_id
        or receipt.definition.job_key != desired_definition.job_key
        or (
            receipt.status == "converged"
            and receipt.definition.definition != desired_definition
        )
    ):
        raise SchedulerInvariantError("scheduler_convergence_receipt_binding_mismatch")
    if receipt.status == "claimed":
        if (
            type(invocation) is not SchedulerClaimedInvocation
            or receipt.claim is None
            or invocation.claim != receipt.claim
            or receipt.claim.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS
        ):
            raise SchedulerInvariantError("scheduler_convergence_claim_policy_violation")
    elif invocation is not None:
        raise SchedulerInvariantError("scheduler_convergence_invocation_is_invalid")
    return receipt, invocation


def _convergence_outcome(
    receipts: tuple[SchedulerDefinitionConvergenceReceiptV1, ...],
) -> Literal["converged", "claimed", "wait", "manual_resolution"]:
    statuses = {receipt.status for receipt in receipts}
    if "manual_resolution" in statuses:
        return "manual_resolution"
    if "wait" in statuses:
        return "wait"
    if "claimed" in statuses:
        return "claimed"
    return "converged"


def _invoke_fail_stop(fail_stop: FailStop, reason: str) -> Never:
    fail_stop(reason)
    raise SchedulerInvocationFailStopReturned(reason)


def _require_uuid(value: object, field_name: str) -> None:
    if not _is_uuid(value):
        raise SchedulerInvariantError(f"{field_name}_is_invalid")


def _is_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _require_reason(value: object, field_name: str) -> None:
    if type(value) is not str or _REASON_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"{field_name}_is_invalid")


def _require_sha256(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SchedulerInvariantError(f"{field_name}_is_invalid")

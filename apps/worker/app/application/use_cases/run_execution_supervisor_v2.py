from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.application.ports.paper_execution_command_source_port import (
    ClaimedPaperExecutionCommand,
    PaperExecutionCommandBundle,
    PaperExecutionCommandSourcePort,
    PaperExecutionSourceCompletion,
    PaperExecutionSourceOutcome,
)
from app.application.services.risk_service import RiskService
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    require_scheduler_invocation_effect_authorization,
)
from app.application.use_cases.run_execution_v2 import (
    ExecutionV2RunOutcome,
    PaperExecutionV2Command,
)
from app.domain.common.time import now_utc
from app.domain.execution_v2.models import ExecutionInvariantError, next_full_minute


class PaperExecutionRunner(Protocol):
    async def execute_paper(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        ...

    async def resume_existing_paper(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        ...

    async def execute_scheduled_paper(
        self,
        command: PaperExecutionV2Command,
        scheduler_authorization: SchedulerInvocationEffectAuthorization,
    ) -> ExecutionV2RunOutcome:
        ...

    async def resume_existing_scheduled_paper(
        self,
        command: PaperExecutionV2Command,
        scheduler_authorization: SchedulerInvocationEffectAuthorization,
    ) -> ExecutionV2RunOutcome:
        ...


@dataclass(frozen=True, slots=True)
class ExecutionSupervisorV2RunResult:
    claimed: int
    new_candidates: int
    resumed: int
    completed: int
    rescheduled: int
    blocked: int
    manual: int
    failed: int = 0


class RunExecutionSupervisorV2:
    """Claim durable Paper inputs and route them through the only V2 executor."""

    def __init__(
        self,
        source: PaperExecutionCommandSourcePort,
        execution: PaperExecutionRunner,
        risk_service: RiskService,
        *,
        worker_id: str,
        current_release_sha: str,
        clock: Callable[[], datetime] = now_utc,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        if not worker_id.strip():
            raise ExecutionInvariantError("paper_supervisor_worker_id_is_required")
        if len(current_release_sha) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in current_release_sha
        ):
            raise ExecutionInvariantError("paper_supervisor_release_sha_is_invalid")
        if lease_ttl <= timedelta(0):
            raise ExecutionInvariantError("paper_supervisor_lease_ttl_is_invalid")
        self.source = source
        self.execution = execution
        self.risk_service = risk_service
        self.worker_id = worker_id
        self.current_release_sha = current_release_sha
        self.clock = clock
        self.lease_ttl = lease_ttl

    async def run_once(self, *, max_items: int = 25) -> ExecutionSupervisorV2RunResult:
        return await self._run_once(
            max_items=max_items,
            scheduler_authorization=None,
        )

    async def run_scheduled(
        self,
        scheduler_authorization: SchedulerInvocationEffectAuthorization,
        *,
        max_items: int = 25,
    ) -> ExecutionSupervisorV2RunResult:
        authorization = _require_execution_scheduler_authorization(
            scheduler_authorization
        )
        return await self._run_once(
            max_items=max_items,
            scheduler_authorization=authorization,
        )

    async def _run_once(
        self,
        *,
        max_items: int,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> ExecutionSupervisorV2RunResult:
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not 1 <= max_items <= 100
        ):
            raise ExecutionInvariantError("paper_supervisor_max_items_is_invalid")
        claimed = 0
        new_candidates = 0
        resumed = 0
        completed = 0
        rescheduled = 0
        blocked = 0
        manual = 0
        failed = 0
        seen: set[str] = set()

        while claimed < max_items:
            claim_now = self._now()
            if scheduler_authorization is None:
                claim = await self.source.claim_available_paper_execution(
                    worker_id=self.worker_id,
                    release_sha=self.current_release_sha,
                    now=claim_now,
                    lease_ttl=self.lease_ttl,
                )
            else:
                authorization = _require_execution_scheduler_authorization(
                    scheduler_authorization
                )
                claim = await self.source.claim_available_paper_execution(
                    worker_id=self.worker_id,
                    release_sha=self.current_release_sha,
                    now=claim_now,
                    lease_ttl=self.lease_ttl,
                    scheduler_authorization=authorization,
                )
            if claim is None:
                break
            claimed += 1
            if claim.command_id in seen:
                raise ExecutionInvariantError("paper_execution_source_repeated_claim")
            seen.add(claim.command_id)
            self._validate_claim(claim, now=claim_now)

            try:
                load_now = self._now()
                if scheduler_authorization is None:
                    bundle = await self.source.load_claimed_paper_execution_bundle(
                        claim,
                        now=load_now,
                    )
                else:
                    authorization = _require_execution_scheduler_authorization(
                        scheduler_authorization
                    )
                    bundle = await self.source.load_claimed_paper_execution_bundle(
                        claim,
                        now=load_now,
                        scheduler_authorization=authorization,
                    )
                self._validate_bundle(claim, bundle, now=self._now())
            except ExecutionInvariantError as exc:
                try:
                    await self._settle(
                        claim,
                        outcome="manual",
                        next_available_at=None,
                        reason_code=_safe_reason(exc.safe_message),
                        scheduler_authorization=scheduler_authorization,
                    )
                except Exception:
                    failed += 1
                else:
                    manual += 1
                continue
            except Exception:
                failed += 1
                continue

            if claim.kind == "new_candidate":
                assert bundle.risk_input is not None
                risk_result = self.risk_service.evaluate_paper_order(bundle.risk_input)
                if not risk_result.allowed:
                    try:
                        await self._settle(
                            claim,
                            outcome="complete",
                            next_available_at=None,
                            reason_code="risk_service_rejected_candidate",
                            scheduler_authorization=scheduler_authorization,
                        )
                    except Exception:
                        failed += 1
                    else:
                        blocked += 1
                    continue
                try:
                    self._validate_allowed_risk_binding(bundle)
                except ExecutionInvariantError as exc:
                    try:
                        await self._settle(
                            claim,
                            outcome="manual",
                            next_available_at=None,
                            reason_code=_safe_reason(exc.safe_message),
                            scheduler_authorization=scheduler_authorization,
                        )
                    except Exception:
                        failed += 1
                    else:
                        manual += 1
                    continue
                new_candidates += 1
                execute_new = True
            else:
                resumed += 1
                execute_new = False

            try:
                if scheduler_authorization is None:
                    if execute_new:
                        outcome = await self.execution.execute_paper(bundle.command)
                    else:
                        outcome = await self.execution.resume_existing_paper(
                            bundle.command
                        )
                else:
                    authorization = _require_execution_scheduler_authorization(
                        scheduler_authorization
                    )
                    if execute_new:
                        outcome = await self.execution.execute_scheduled_paper(
                            bundle.command,
                            authorization,
                        )
                    else:
                        outcome = (
                            await self.execution.resume_existing_scheduled_paper(
                                bundle.command,
                                authorization,
                            )
                        )
            except Exception:
                # Do not acknowledge an ambiguous execution boundary. The source
                # lease expires and a durable replay starts from the DB checkpoint.
                failed += 1
                continue

            try:
                if outcome.status == "pending":
                    next_available_at = _next_available_at(
                        bundle.command,
                        now=self._now(),
                    )
                    await self._settle(
                        claim,
                        outcome="reschedule",
                        next_available_at=next_available_at,
                        reason_code="paper_execution_waiting_for_next_eligible_bar",
                        scheduler_authorization=scheduler_authorization,
                    )
                    rescheduled += 1
                elif outcome.status == "quarantined":
                    await self._settle(
                        claim,
                        outcome="manual",
                        next_available_at=None,
                        reason_code=_safe_reason(
                            outcome.reason_code or "paper_execution_quarantined"
                        ),
                        scheduler_authorization=scheduler_authorization,
                    )
                    manual += 1
                else:
                    await self._settle(
                        claim,
                        outcome="complete",
                        next_available_at=None,
                        reason_code=(
                            "paper_execution_semantic_duplicate"
                            if outcome.status == "duplicate_semantic_intent"
                            else "paper_execution_completed"
                        ),
                        scheduler_authorization=scheduler_authorization,
                    )
                    completed += 1
            except Exception:
                failed += 1

        return ExecutionSupervisorV2RunResult(
            claimed=claimed,
            new_candidates=new_candidates,
            resumed=resumed,
            completed=completed,
            rescheduled=rescheduled,
            blocked=blocked,
            manual=manual,
            failed=failed,
        )

    def _validate_claim(
        self,
        claim: ClaimedPaperExecutionCommand,
        *,
        now: datetime,
    ) -> None:
        if claim.worker_id != self.worker_id:
            raise ExecutionInvariantError("paper_execution_claim_worker_mismatch")
        if claim.release_sha != self.current_release_sha:
            raise ExecutionInvariantError("paper_execution_claim_release_mismatch")
        if claim.claim_expires_at <= now:
            raise ExecutionInvariantError("paper_execution_claim_is_expired")

    def _validate_bundle(
        self,
        claim: ClaimedPaperExecutionCommand,
        bundle: PaperExecutionCommandBundle,
        *,
        now: datetime,
    ) -> None:
        command = bundle.command
        if command.intent.id != claim.intent_id:
            raise ExecutionInvariantError("paper_execution_bundle_intent_mismatch")
        if command.intent.environment != "paper":
            raise ExecutionInvariantError("paper_execution_supervisor_requires_paper")
        if command.intent.lease_holder_id != self.worker_id:
            raise ExecutionInvariantError("paper_execution_bundle_worker_mismatch")
        if command.evaluated_at > now:
            raise ExecutionInvariantError("paper_execution_bundle_uses_future_evidence")
        if command.evaluated_at < claim.available_at:
            raise ExecutionInvariantError("paper_execution_bundle_precedes_availability")
        if claim.kind == "new_candidate" and bundle.risk_input is None:
            raise ExecutionInvariantError("paper_execution_new_candidate_requires_risk_input")
        if (
            claim.kind == "new_candidate"
            and command.intent.risk_expires_at <= now
        ):
            raise ExecutionInvariantError("paper_execution_risk_evidence_expired")
        if claim.kind == "resume_existing":
            if bundle.risk_input is not None:
                raise ExecutionInvariantError("paper_execution_resume_risk_input_is_unexpected")
            if command.dispatch_at != command.evaluated_at:
                raise ExecutionInvariantError("paper_execution_resume_requires_current_dispatch")

    def _validate_allowed_risk_binding(
        self,
        bundle: PaperExecutionCommandBundle,
    ) -> None:
        risk_input = bundle.risk_input
        assert risk_input is not None
        intent = bundle.command.intent
        if risk_input.settings.mode != "paper":
            raise ExecutionInvariantError("paper_execution_risk_mode_mismatch")
        if risk_input.signal.action != intent.side:
            raise ExecutionInvariantError("paper_execution_risk_action_mismatch")
        if risk_input.signal.symbol != intent.symbol:
            raise ExecutionInvariantError("paper_execution_risk_symbol_mismatch")
        if (
            risk_input.strategy_version_id is None
            or str(risk_input.strategy_version_id) != intent.strategy_version_id
        ):
            raise ExecutionInvariantError("paper_execution_risk_strategy_mismatch")
        if risk_input.now != intent.risk_evaluated_at:
            raise ExecutionInvariantError("paper_execution_risk_timestamp_mismatch")
        if intent.risk_reason_codes:
            raise ExecutionInvariantError("paper_execution_allowed_risk_has_reason_codes")
        if intent.quantity * intent.limit_price_krw > risk_input.signal.order_amount_krw:
            raise ExecutionInvariantError("paper_execution_notional_exceeds_risk_input")

    async def _settle(
        self,
        claim: ClaimedPaperExecutionCommand,
        *,
        outcome: PaperExecutionSourceOutcome,
        next_available_at: datetime | None,
        reason_code: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> PaperExecutionSourceCompletion:
        completion_now = self._now()
        if scheduler_authorization is None:
            completion = await self.source.complete_or_reschedule_paper_execution(
                command_id=claim.command_id,
                claim_token=claim.claim_token,
                expected_revision=claim.source_revision,
                worker_id=self.worker_id,
                release_sha=self.current_release_sha,
                now=completion_now,
                outcome=outcome,
                next_available_at=next_available_at,
                reason_code=reason_code,
            )
        else:
            authorization = _require_execution_scheduler_authorization(
                scheduler_authorization
            )
            completion = await self.source.complete_or_reschedule_paper_execution(
                command_id=claim.command_id,
                claim_token=claim.claim_token,
                expected_revision=claim.source_revision,
                worker_id=self.worker_id,
                release_sha=self.current_release_sha,
                now=completion_now,
                outcome=outcome,
                next_available_at=next_available_at,
                reason_code=reason_code,
                scheduler_authorization=authorization,
            )
        expected_state = {
            "complete": "complete",
            "reschedule": "pending",
            "manual": "manual",
        }[outcome]
        if completion.command_id != claim.command_id:
            raise ExecutionInvariantError("paper_execution_completion_identity_mismatch")
        if completion.state != expected_state:
            raise ExecutionInvariantError("paper_execution_completion_state_mismatch")
        if completion.source_revision != claim.source_revision + 1:
            raise ExecutionInvariantError("paper_execution_completion_revision_is_invalid")
        if completion.next_available_at != next_available_at:
            raise ExecutionInvariantError("paper_execution_completion_schedule_mismatch")
        return completion

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionInvariantError("paper_supervisor_clock_must_be_timezone_aware")
        return value


def _next_available_at(
    command: PaperExecutionV2Command,
    *,
    now: datetime,
) -> datetime:
    if now >= command.intent.expires_at:
        raise ExecutionInvariantError("paper_execution_pending_at_or_after_expiry")
    next_at = min(next_full_minute(now), command.intent.expires_at)
    if next_at <= now:
        raise ExecutionInvariantError("paper_execution_next_bar_is_not_in_future")
    return next_at


def _safe_reason(value: str) -> str:
    normalized = value.strip()
    return (normalized or "paper_execution_invariant_failed")[:120]


def _require_execution_scheduler_authorization(
    value: object,
) -> SchedulerInvocationEffectAuthorization:
    return require_scheduler_invocation_effect_authorization(
        value,
        expected_job_key="operations.execution",
    )

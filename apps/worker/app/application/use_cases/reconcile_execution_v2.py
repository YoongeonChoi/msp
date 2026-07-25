from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.application.ports.execution_reconciliation_port import (
    ExecutionReconciliationHandlerPort,
    ExecutionReconciliationPort,
    PreDispatchFailurePort,
)
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    require_scheduler_invocation_effect_authorization,
)
from app.domain.common.time import now_utc
from app.domain.execution_v2.models import (
    ExecutionInvariantError,
    WorkerLease,
    next_full_minute,
)
from app.domain.execution_v2.reconciliation import (
    ExecutionReconciliationClaim,
    ExecutionReconciliationDecision,
)

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationRunResult:
    claimed: int
    completed: int
    rescheduled: int
    manual: int
    failed: int = 0
    unknown_listed: int = 0
    unknown_claimed: int = 0
    unknown_resumed: int = 0
    unknown_applied: int = 0
    unknown_replayed: int = 0
    unknown_failed: int = 0


class ReconcileExecutionV2:
    """Priority/keyset recovery runner with restart-safe leased claims."""

    def __init__(
        self,
        port: ExecutionReconciliationPort,
        handler: ExecutionReconciliationHandlerPort,
        *,
        account_id: str,
        worker_id: str,
        current_release_sha: str,
        lease_provider: Callable[[], WorkerLease | None],
        clock: Callable[[], datetime] = now_utc,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        try:
            parsed_worker_id = UUID(worker_id)
        except (TypeError, ValueError) as exc:
            raise ExecutionInvariantError("reconciliation_worker_id_is_invalid") from exc
        if str(parsed_worker_id) != worker_id or parsed_worker_id.version not in {1, 2, 3, 4, 5}:
            raise ExecutionInvariantError("reconciliation_worker_id_is_invalid")
        if lease_ttl <= timedelta(0):
            raise ExecutionInvariantError("reconciliation_lease_ttl_is_invalid")
        if not account_id.strip():
            raise ExecutionInvariantError("reconciliation_account_id_is_required")
        if _RELEASE_SHA_RE.fullmatch(current_release_sha) is None:
            raise ExecutionInvariantError("reconciliation_release_sha_is_invalid")
        self.port = port
        self.handler = handler
        self.account_id = account_id
        self.worker_id = worker_id
        self.current_release_sha = current_release_sha
        self.lease_provider = lease_provider
        self.clock = clock
        self.lease_ttl = lease_ttl

    async def run_once(self, *, max_items: int = 500) -> ExecutionReconciliationRunResult:
        return await self._run_once(max_items=max_items, authorization=None)

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
        *,
        max_items: int = 500,
    ) -> ExecutionReconciliationRunResult:
        _require_reconciliation_effect(authorization)
        return await self._run_once(
            max_items=max_items,
            authorization=authorization,
        )

    async def _run_once(
        self,
        *,
        max_items: int,
        authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> ExecutionReconciliationRunResult:
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not 1 <= max_items <= 10_000
        ):
            raise ExecutionInvariantError("reconciliation_max_items_is_invalid")
        cursor: tuple[int, str] | None = None
        seen: set[str] = set()
        claimed_count = 0
        completed = 0
        rescheduled = 0
        manual = 0
        failed = 0

        while claimed_count < max_items:
            page_limit = min(50, max_items - claimed_count)
            claimed_at = self._now()
            lease = self._current_lease(claimed_at)
            if authorization is None:
                page = await self.port.claim_execution_reconciliation_batch(
                    account_id=self.account_id,
                    worker_id=self.worker_id,
                    release_sha=self.current_release_sha,
                    fencing_token=lease.fencing_token,
                    now=claimed_at,
                    limit=page_limit,
                    after_priority=cursor[0] if cursor is not None else None,
                    after_intent_id=cursor[1] if cursor is not None else None,
                    lease_ttl=self.lease_ttl,
                )
            else:
                _require_reconciliation_effect(authorization)
                page = await self.port.claim_execution_reconciliation_batch(
                    account_id=self.account_id,
                    worker_id=self.worker_id,
                    release_sha=self.current_release_sha,
                    fencing_token=lease.fencing_token,
                    now=claimed_at,
                    limit=page_limit,
                    after_priority=cursor[0] if cursor is not None else None,
                    after_intent_id=cursor[1] if cursor is not None else None,
                    lease_ttl=self.lease_ttl,
                    scheduler_authorization=authorization,
                )
            if not page:
                break
            page_cursors = tuple(item.cursor for item in page)
            if page_cursors != tuple(sorted(page_cursors)):
                raise ExecutionInvariantError("reconciliation_claim_page_is_not_sorted")
            if cursor is not None and page_cursors[0] <= cursor:
                raise ExecutionInvariantError("reconciliation_claim_cursor_did_not_advance")
            if any(item.intent_id in seen for item in page):
                raise ExecutionInvariantError("reconciliation_claim_was_duplicated")
            if any(item.lease_expires_at <= claimed_at for item in page):
                raise ExecutionInvariantError("reconciliation_claim_lease_is_expired")
            if any(
                item.account_id != self.account_id
                or item.lease_release_sha != self.current_release_sha
                or item.lease_fencing_token != lease.fencing_token
                for item in page
            ):
                raise ExecutionInvariantError("reconciliation_claim_gate_mismatch")

            for claim in page:
                seen.add(claim.intent_id)
                try:
                    if authorization is None:
                        decision = await self.handler.reconcile_claim(
                            claim,
                            now=self._now(),
                        )
                    else:
                        _require_reconciliation_effect(authorization)
                        decision = await self.handler.reconcile_claim(
                            claim,
                            now=self._now(),
                            scheduler_authorization=authorization,
                        )
                except Exception as exc:
                    decision = ExecutionReconciliationDecision(
                        "manual",
                        _safe_reconciliation_failure_code("handler", exc),
                    )
                if not decision.completion_persisted:
                    try:
                        if authorization is None:
                            completion = (
                                await self.port.complete_execution_reconciliation(
                                    intent_id=claim.intent_id,
                                    worker_id=self.worker_id,
                                    release_sha=claim.lease_release_sha,
                                    fencing_token=claim.lease_fencing_token,
                                    now=self._now(),
                                    outcome=decision.outcome,
                                    next_reconcile_at=decision.next_reconcile_at,
                                    reason_code=decision.reason_code,
                                )
                            )
                        else:
                            _require_reconciliation_effect(authorization)
                            completion = (
                                await self.port.complete_execution_reconciliation(
                                    intent_id=claim.intent_id,
                                    worker_id=self.worker_id,
                                    release_sha=claim.lease_release_sha,
                                    fencing_token=claim.lease_fencing_token,
                                    now=self._now(),
                                    outcome=decision.outcome,
                                    next_reconcile_at=decision.next_reconcile_at,
                                    reason_code=decision.reason_code,
                                    scheduler_authorization=authorization,
                                )
                            )
                        if completion.intent_id != claim.intent_id:
                            raise ExecutionInvariantError(
                                "reconciliation_completion_identity_mismatch"
                            )
                        expected_state = {
                            "reschedule": "pending",
                            "complete": "complete",
                            "manual": "manual",
                        }[decision.outcome]
                        if completion.state != expected_state:
                            raise ExecutionInvariantError(
                                "reconciliation_completion_postcondition_mismatch"
                            )
                    except Exception:
                        failed += 1
                        continue
                if decision.outcome == "reschedule":
                    rescheduled += 1
                elif decision.outcome == "complete":
                    completed += 1
                else:
                    manual += 1
            claimed_count += len(page)
            cursor = page[-1].cursor
            if len(page) < page_limit:
                break

        return ExecutionReconciliationRunResult(
            claimed=claimed_count,
            completed=completed,
            rescheduled=rescheduled,
            manual=manual,
            failed=failed,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionInvariantError("reconciliation_clock_must_be_timezone_aware")
        return value

    def _current_lease(self, now: datetime) -> WorkerLease:
        lease = self.lease_provider()
        if (
            lease is None
            or lease.account_id != self.account_id
            or lease.holder_id != self.worker_id
            or not lease.is_active(now)
        ):
            raise ExecutionInvariantError("reconciliation_worker_lease_is_not_current")
        return lease


def _safe_reconciliation_failure_code(stage: str, exc: Exception) -> str:
    return f"{stage}_{type(exc).__name__.lower()}"[:120]


class FailClosedExecutionReconciliationHandler:
    """Resume safe paper states and quarantine evidence-incomplete dispatches."""

    def __init__(
        self,
        recovery: PreDispatchFailurePort,
        *,
        worker_id: str,
        current_release_sha: str,
    ) -> None:
        try:
            parsed_worker_id = UUID(worker_id)
        except (TypeError, ValueError) as exc:
            raise ExecutionInvariantError("reconciliation_worker_id_is_invalid") from exc
        if str(parsed_worker_id) != worker_id:
            raise ExecutionInvariantError("reconciliation_worker_id_is_invalid")
        if len(current_release_sha) not in {40, 64} or any(
            character not in "0123456789abcdef"
            for character in current_release_sha
        ):
            raise ExecutionInvariantError("reconciliation_release_sha_is_invalid")
        self.recovery = recovery
        self.worker_id = worker_id
        self.current_release_sha = current_release_sha

    async def reconcile_claim(
        self,
        claim: ExecutionReconciliationClaim,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ExecutionReconciliationDecision:
        if claim.lease_release_sha != self.current_release_sha:
            raise ExecutionInvariantError(
                "reconciliation_current_release_lease_mismatch"
            )
        if claim.recovery_disposition == "manual_release_takeover":
            return ExecutionReconciliationDecision(
                "manual",
                f"{claim.environment}_dispatch_release_takeover_requires_manual",
            )
        if claim.attempt_id is None and claim.latest_observation_id is None:
            reason_code = (
                "worker_restart_before_dispatch"
                if claim.intent_release_sha == self.current_release_sha
                else "worker_restart_before_dispatch_release_takeover"
            )
            if scheduler_authorization is None:
                result = await self.recovery.fail_reserved_intent_pre_dispatch(
                    intent_id=claim.intent_id,
                    worker_id=self.worker_id,
                    fencing_token=claim.lease_fencing_token,
                    control_epoch=claim.control_epoch,
                    release_sha=self.current_release_sha,
                    now=now,
                    reason_code=reason_code,
                )
            else:
                _require_reconciliation_effect(scheduler_authorization)
                result = await self.recovery.fail_reserved_intent_pre_dispatch(
                    intent_id=claim.intent_id,
                    worker_id=self.worker_id,
                    fencing_token=claim.lease_fencing_token,
                    control_epoch=claim.control_epoch,
                    release_sha=self.current_release_sha,
                    now=now,
                    reason_code=reason_code,
                    scheduler_authorization=scheduler_authorization,
                )
            if result.intent_id != claim.intent_id:
                raise ExecutionInvariantError("pre_dispatch_failure_identity_mismatch")
            if result.state != "complete" or result.reason_code != reason_code:
                raise ExecutionInvariantError("pre_dispatch_failure_postcondition_invalid")
            return ExecutionReconciliationDecision(
                "complete",
                reason_code,
                completion_persisted=True,
            )
        if claim.intent_release_sha != self.current_release_sha:
            raise ExecutionInvariantError(
                "reconciliation_release_takeover_disposition_is_invalid"
            )
        if claim.environment == "paper":
            if now < claim.expires_at:
                return ExecutionReconciliationDecision(
                    "reschedule",
                    "paper_execution_waiting_for_next_eligible_bar",
                    next_reconcile_at=min(next_full_minute(now), claim.expires_at),
                )
            reason_code = "paper_day_limit_remainder_expired_after_recovery"
            if scheduler_authorization is None:
                expiry_result = await self.recovery.expire_paper_intent_remainder(
                    intent_id=claim.intent_id,
                    worker_id=self.worker_id,
                    fencing_token=claim.lease_fencing_token,
                    control_epoch=claim.control_epoch,
                    release_sha=self.current_release_sha,
                    now=now,
                    reason_code=reason_code,
                )
            else:
                _require_reconciliation_effect(scheduler_authorization)
                expiry_result = await self.recovery.expire_paper_intent_remainder(
                    intent_id=claim.intent_id,
                    worker_id=self.worker_id,
                    fencing_token=claim.lease_fencing_token,
                    control_epoch=claim.control_epoch,
                    release_sha=self.current_release_sha,
                    now=now,
                    reason_code=reason_code,
                    scheduler_authorization=scheduler_authorization,
                )
            if (
                expiry_result.intent_id != claim.intent_id
                or expiry_result.state != "complete"
                or expiry_result.reason_code != reason_code
                or expiry_result.sequence != (claim.latest_sequence or 0) + 1
            ):
                raise ExecutionInvariantError(
                    "paper_expiry_recovery_postcondition_invalid"
                )
            return ExecutionReconciliationDecision(
                "complete",
                reason_code,
                completion_persisted=True,
            )
        return ExecutionReconciliationDecision(
            "manual",
            f"{claim.environment}_dispatch_replay_evidence_unavailable",
        )


def _require_reconciliation_effect(
    authorization: object,
) -> SchedulerInvocationEffectAuthorization:
    return require_scheduler_invocation_effect_authorization(
        authorization,
        expected_job_key="operations.reconciliation",
    )

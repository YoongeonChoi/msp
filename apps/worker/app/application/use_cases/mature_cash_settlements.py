from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.application.ports.cash_settlement_port import CashSettlementPort
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    require_scheduler_invocation_effect_authorization,
)
from app.domain.common.time import now_utc
from app.domain.execution_v2.cash_settlement import (
    CashSettlementClaim,
    CashSettlementCompletionAmbiguousError,
    CashSettlementCompletionRetryableError,
    CashSettlementReceipt,
)
from app.domain.execution_v2.models import ExecutionInvariantError, WorkerLease

_MAX_SETTLEMENTS_PER_RUN = 3


@dataclass(frozen=True, slots=True)
class CashSettlementRunResult:
    claimed: int
    completed: int
    replayed: int
    retried: int
    dead_lettered: int
    failed: int


class MatureCashSettlements:
    """Mature due cash obligations under the scheduler's current fence."""

    def __init__(
        self,
        port: CashSettlementPort,
        *,
        account_id: str,
        environment: Literal["paper", "contract_test"],
        holder_id: str,
        release_sha: str,
        lease_provider: Callable[[], WorkerLease | None],
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if not account_id.strip() or not holder_id.strip() or not release_sha.strip():
            raise ExecutionInvariantError("cash_settlement_runner_identity_is_required")
        if environment not in {"paper", "contract_test"}:
            raise ExecutionInvariantError("cash_settlement_runner_environment_is_invalid")
        self.port = port
        self.account_id = account_id
        self.environment = environment
        self.holder_id = holder_id
        self.release_sha = release_sha
        self.lease_provider = lease_provider
        self.clock = clock

    async def run_once(self) -> CashSettlementRunResult:
        return await self._run(scheduler_authorization=None)

    async def run_scheduled(
        self,
        scheduler_authorization: SchedulerInvocationEffectAuthorization,
    ) -> CashSettlementRunResult:
        authorization = require_scheduler_invocation_effect_authorization(
            scheduler_authorization,
            expected_job_key="operations.settlement",
        )
        return await self._run(scheduler_authorization=authorization)

    async def _run(
        self,
        *,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> CashSettlementRunResult:
        claimed = completed = replayed = retried = dead_lettered = failed = 0
        for _ in range(_MAX_SETTLEMENTS_PER_RUN):
            claim_time = self._now()
            lease = self._current_lease(claim_time)
            if scheduler_authorization is None:
                claims = await self.port.claim_cash_settlement_batch(
                    account_id=self.account_id,
                    holder_id=self.holder_id,
                    release_sha=self.release_sha,
                    fencing_token=lease.fencing_token,
                    now=claim_time,
                    limit=1,
                )
            else:
                authorization = require_scheduler_invocation_effect_authorization(
                    scheduler_authorization,
                    expected_job_key="operations.settlement",
                )
                claims = await self.port.claim_cash_settlement_batch(
                    account_id=self.account_id,
                    holder_id=self.holder_id,
                    release_sha=self.release_sha,
                    fencing_token=lease.fencing_token,
                    now=claim_time,
                    limit=1,
                    scheduler_authorization=authorization,
                )
            if len(claims) > 1:
                raise ExecutionInvariantError(
                    "cash_settlement_claim_batch_is_not_singleton"
                )
            if not claims:
                break
            claim = claims[0]
            claimed += 1
            if (
                claim.account_id != self.account_id
                or claim.environment != self.environment
            ):
                raise ExecutionInvariantError("cash_settlement_claim_scope_mismatch")
            operation_time = self._now()
            lease = self._current_lease(operation_time)
            try:
                receipt = await self._complete_with_exact_replay(
                    claim,
                    lease,
                    operation_time,
                    scheduler_authorization=scheduler_authorization,
                )
            except CashSettlementCompletionRetryableError as exc:
                failure_time = self._now()
                lease = self._current_lease(failure_time)
                if scheduler_authorization is None:
                    failure = await self.port.fail_cash_settlement_attempt(
                        claim,
                        holder_id=self.holder_id,
                        release_sha=self.release_sha,
                        fencing_token=lease.fencing_token,
                        now=failure_time,
                        error_code=exc.failure_code,
                    )
                else:
                    authorization = require_scheduler_invocation_effect_authorization(
                        scheduler_authorization,
                        expected_job_key="operations.settlement",
                    )
                    failure = await self.port.fail_cash_settlement_attempt(
                        claim,
                        holder_id=self.holder_id,
                        release_sha=self.release_sha,
                        fencing_token=lease.fencing_token,
                        now=failure_time,
                        error_code=exc.failure_code,
                        scheduler_authorization=authorization,
                    )
                failed += 1
                if failure.replayed:
                    replayed += 1
                if failure.state == "dead_letter":
                    dead_lettered += 1
                else:
                    retried += 1
                continue
            completed += 1
            if receipt.replayed:
                replayed += 1
        return CashSettlementRunResult(
            claimed=claimed,
            completed=completed,
            replayed=replayed,
            retried=retried,
            dead_lettered=dead_lettered,
            failed=failed,
        )

    async def _complete_with_exact_replay(
        self,
        claim: CashSettlementClaim,
        lease: WorkerLease,
        operation_time: datetime,
        *,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> CashSettlementReceipt:
        try:
            if scheduler_authorization is None:
                return await self.port.complete_cash_settlement(
                    claim,
                    holder_id=self.holder_id,
                    release_sha=self.release_sha,
                    fencing_token=lease.fencing_token,
                    now=operation_time,
                )
            authorization = require_scheduler_invocation_effect_authorization(
                scheduler_authorization,
                expected_job_key="operations.settlement",
            )
            return await self.port.complete_cash_settlement(
                claim,
                holder_id=self.holder_id,
                release_sha=self.release_sha,
                fencing_token=lease.fencing_token,
                now=operation_time,
                scheduler_authorization=authorization,
            )
        except CashSettlementCompletionAmbiguousError:
            replay_time = self._now()
            replay_lease = self._current_lease(replay_time)
            if scheduler_authorization is None:
                return await self.port.complete_cash_settlement(
                    claim,
                    holder_id=self.holder_id,
                    release_sha=self.release_sha,
                    fencing_token=replay_lease.fencing_token,
                    now=replay_time,
                )
            authorization = require_scheduler_invocation_effect_authorization(
                scheduler_authorization,
                expected_job_key="operations.settlement",
            )
            return await self.port.complete_cash_settlement(
                claim,
                holder_id=self.holder_id,
                release_sha=self.release_sha,
                fencing_token=replay_lease.fencing_token,
                now=replay_time,
                scheduler_authorization=authorization,
            )

    def _current_lease(self, now: datetime) -> WorkerLease:
        lease = self.lease_provider()
        if lease is None:
            raise ExecutionInvariantError("cash_settlement_worker_lease_is_missing")
        if (
            lease.account_id != self.account_id
            or lease.holder_id != self.holder_id
            or not lease.is_active(now)
        ):
            raise ExecutionInvariantError("cash_settlement_worker_lease_is_stale")
        return lease

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionInvariantError("cash_settlement_clock_must_be_timezone_aware")
        return value

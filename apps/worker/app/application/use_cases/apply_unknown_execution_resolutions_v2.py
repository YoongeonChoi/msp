from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.application.ports.unknown_resolution_port import UnknownResolutionPort
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    require_scheduler_invocation_effect_authorization,
)
from app.application.use_cases.reconcile_execution_v2 import (
    ExecutionReconciliationRunResult,
)
from app.domain.common.time import now_utc
from app.domain.execution_v2.models import (
    ExecutionEnvironment,
    ExecutionInvariantError,
    WorkerLease,
)
from app.domain.execution_v2.unknown_resolution import (
    UnknownResolutionApplicationReceipt,
    UnknownResolutionApplyAmbiguousError,
    UnknownResolutionCandidate,
    UnknownResolutionClaim,
)

_MAX_UNKNOWN_RESOLUTIONS_PER_RUN = 3


@dataclass(frozen=True, slots=True)
class UnknownResolutionRunResult:
    listed: int
    claimed: int
    resumed: int
    applied: int
    replayed: int
    failed: int


class ApplyUnknownExecutionResolutionsV2:
    """Apply only human-approved unknown closures under the current worker fence.

    The dedicated list/claim/apply protocol is intentionally independent from
    generic operation command acknowledgement.  A lost apply response is
    checked once using the deterministic post-apply revisions; the mutating
    request itself is never blindly retried.
    """

    def __init__(
        self,
        port: UnknownResolutionPort,
        *,
        account_id: str,
        environment: ExecutionEnvironment,
        holder_id: str,
        release_sha: str,
        lease_provider: Callable[[], WorkerLease | None],
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if not account_id.strip() or not holder_id.strip() or not release_sha.strip():
            raise ExecutionInvariantError("unknown_resolution_runner_identity_is_required")
        if environment not in {"paper", "contract_test"}:
            raise ExecutionInvariantError("unknown_resolution_runner_environment_is_invalid")
        self.port = port
        self.account_id = account_id
        self.environment = environment
        self.holder_id = holder_id
        self.release_sha = release_sha
        self.lease_provider = lease_provider
        self.clock = clock

    async def run_once(self) -> UnknownResolutionRunResult:
        return await self._run_once(None)

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> UnknownResolutionRunResult:
        _require_reconciliation_effect(authorization)
        return await self._run_once(authorization)

    async def _run_once(
        self,
        authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> UnknownResolutionRunResult:
        listed_at = self._now()
        lease = self._current_lease(listed_at)
        if authorization is None:
            candidates = await self.port.list_unknown_resolution_candidates(
                account_id=self.account_id,
                environment=self.environment,
                holder_id=self.holder_id,
                release_sha=self.release_sha,
                fencing_token=lease.fencing_token,
                now=listed_at,
                limit=_MAX_UNKNOWN_RESOLUTIONS_PER_RUN,
            )
        else:
            _require_reconciliation_effect(authorization)
            candidates = await self.port.list_unknown_resolution_candidates(
                account_id=self.account_id,
                environment=self.environment,
                holder_id=self.holder_id,
                release_sha=self.release_sha,
                fencing_token=lease.fencing_token,
                now=listed_at,
                limit=_MAX_UNKNOWN_RESOLUTIONS_PER_RUN,
                scheduler_authorization=authorization,
            )
        if len(candidates) > _MAX_UNKNOWN_RESOLUTIONS_PER_RUN:
            raise ExecutionInvariantError("unknown_resolution_list_exceeds_run_bound")
        if any(
            len({getattr(candidate, field) for candidate in candidates}) != len(candidates)
            for field in ("command_id", "break_id", "intent_id")
        ):
            raise ExecutionInvariantError("unknown_resolution_list_contains_duplicates")

        claimed = resumed = applied = replayed = failed = 0
        for candidate in candidates:
            try:
                self._validate_candidate_scope(candidate, lease.fencing_token)
                claim_time = self._now()
                lease = self._current_lease(claim_time)
                self._require_same_fence(candidate.lease_fencing_token, lease)
                if candidate.has_active_owned_claim(claim_time):
                    claim = _resume_claim(candidate)
                    resumed += 1
                    self._validate_claim(
                        candidate,
                        claim,
                        claim_time,
                        newly_claimed=False,
                    )
                else:
                    if authorization is None:
                        claim = await self.port.claim_unknown_resolution(
                            candidate,
                            now=claim_time,
                        )
                    else:
                        _require_reconciliation_effect(authorization)
                        claim = await self.port.claim_unknown_resolution(
                            candidate,
                            now=claim_time,
                            scheduler_authorization=authorization,
                        )
                    self._validate_claim(
                        candidate,
                        claim,
                        claim_time,
                        newly_claimed=True,
                    )
                    claimed += 1
                apply_time = self._now()
                lease = self._current_lease(apply_time)
                self._require_same_fence(claim.lease_fencing_token, lease)
                receipt = await self._apply_with_exact_post_state_replay(
                    claim,
                    apply_time,
                    authorization,
                )
                self._validate_receipt(claim, receipt)
                applied += 1
                if receipt.replayed:
                    replayed += 1
            except Exception:
                failed += 1

        return UnknownResolutionRunResult(
            listed=len(candidates),
            claimed=claimed,
            resumed=resumed,
            applied=applied,
            replayed=replayed,
            failed=failed,
        )

    async def _apply_with_exact_post_state_replay(
        self,
        claim: UnknownResolutionClaim,
        apply_time: datetime,
        authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> UnknownResolutionApplicationReceipt:
        try:
            if authorization is None:
                receipt = await self.port.apply_unknown_resolution(
                    claim,
                    now=apply_time,
                    replay=False,
                )
            else:
                _require_reconciliation_effect(authorization)
                receipt = await self.port.apply_unknown_resolution(
                    claim,
                    now=apply_time,
                    replay=False,
                    scheduler_authorization=authorization,
                )
            if receipt.inserted is not True:
                raise UnknownResolutionApplyAmbiguousError
            return receipt
        except UnknownResolutionApplyAmbiguousError:
            replay_time = self._now()
            replay_lease = self._current_lease(replay_time)
            self._require_same_fence(claim.lease_fencing_token, replay_lease)
            if authorization is None:
                receipt = await self.port.apply_unknown_resolution(
                    claim,
                    now=replay_time,
                    replay=True,
                )
            else:
                _require_reconciliation_effect(authorization)
                receipt = await self.port.apply_unknown_resolution(
                    claim,
                    now=replay_time,
                    replay=True,
                    scheduler_authorization=authorization,
                )
            if receipt.inserted is not False:
                raise UnknownResolutionApplyAmbiguousError from None
            return receipt

    def _validate_candidate_scope(
        self,
        candidate: UnknownResolutionCandidate,
        fencing_token: int,
    ) -> None:
        if (
            candidate.account_id != self.account_id
            or candidate.environment != self.environment
            or candidate.holder_id != self.holder_id
            or candidate.release_sha != self.release_sha
            or candidate.lease_fencing_token != fencing_token
        ):
            raise ExecutionInvariantError("unknown_resolution_candidate_scope_mismatch")

    @staticmethod
    def _validate_claim(
        candidate: UnknownResolutionCandidate,
        claim: UnknownResolutionClaim,
        now: datetime,
        *,
        newly_claimed: bool,
    ) -> None:
        revision_delta = 1 if newly_claimed else 0
        if (
            claim.command_id != candidate.command_id
            or claim.request_id != candidate.request_id
            or claim.review_id != candidate.review_id
            or claim.break_id != candidate.break_id
            or claim.intent_id != candidate.intent_id
            or claim.terminal_status != candidate.terminal_status
            or claim.request_payload_sha256 != candidate.request_payload_sha256
            or claim.review_payload_sha256 != candidate.review_payload_sha256
            or claim.command_revision != candidate.command_revision + revision_delta
            or claim.work_revision != candidate.work_revision + revision_delta
            or claim.expected_control_epoch != candidate.expected_control_epoch
            or claim.account_id != candidate.account_id
            or claim.environment != candidate.environment
            or claim.holder_id != candidate.holder_id
            or claim.release_sha != candidate.release_sha
            or claim.lease_fencing_token != candidate.lease_fencing_token
            or claim.claim_expires_at <= now
        ):
            raise ExecutionInvariantError("unknown_resolution_claim_binding_mismatch")

    @staticmethod
    def _validate_receipt(
        claim: UnknownResolutionClaim,
        receipt: UnknownResolutionApplicationReceipt,
    ) -> None:
        if (
            receipt.command_id != claim.command_id
            or receipt.break_id != claim.break_id
            or receipt.intent_id != claim.intent_id
            or receipt.terminal_status != claim.terminal_status
            or receipt.request_digest_sha256 != claim.request_payload_sha256
            or receipt.review_digest_sha256 != claim.review_payload_sha256
            or receipt.claim_token != claim.claim_token
            or receipt.receipt_revision != claim.command_revision + 1
            or receipt.work_revision != claim.work_revision + 1
            or receipt.account_id != claim.account_id
            or receipt.environment != claim.environment
            or receipt.holder_id != claim.holder_id
            or receipt.release_sha != claim.release_sha
            or receipt.lease_fencing_token != claim.lease_fencing_token
            or receipt.control_epoch != claim.expected_control_epoch
        ):
            raise ExecutionInvariantError("unknown_resolution_receipt_binding_mismatch")

    def _current_lease(self, now: datetime) -> WorkerLease:
        lease = self.lease_provider()
        if lease is None:
            raise ExecutionInvariantError("unknown_resolution_worker_lease_is_missing")
        if (
            lease.account_id != self.account_id
            or lease.holder_id != self.holder_id
            or not lease.is_active(now)
        ):
            raise ExecutionInvariantError("unknown_resolution_worker_lease_is_stale")
        return lease

    @staticmethod
    def _require_same_fence(expected: int, lease: WorkerLease) -> None:
        if lease.fencing_token != expected:
            raise ExecutionInvariantError("unknown_resolution_worker_fence_changed")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionInvariantError("unknown_resolution_clock_must_be_timezone_aware")
        return value


class ExecutionReconciliationRunner(Protocol):
    async def run_once(self) -> ExecutionReconciliationRunResult: ...

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> ExecutionReconciliationRunResult: ...


class UnknownResolutionRunner(Protocol):
    async def run_once(self) -> UnknownResolutionRunResult: ...

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> UnknownResolutionRunResult: ...


class RunExecutionReconciliationStageV2:
    """Resolve approved manual breaks before running generic reconciliation.

    Existing approved resolutions are applied first so their reservations and
    projections reach a reviewed terminal state before the generic scanner
    observes the account.  Both sub-runners are attempted independently; a
    boundary-level failure still fails the shared scheduler stage.
    """

    def __init__(
        self,
        unknown: UnknownResolutionRunner,
        generic: ExecutionReconciliationRunner,
    ) -> None:
        self.unknown = unknown
        self.generic = generic

    async def run_once(self) -> ExecutionReconciliationRunResult:
        return await self._run_once(None)

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> ExecutionReconciliationRunResult:
        _require_reconciliation_effect(authorization)
        return await self._run_once(authorization)

    async def _run_once(
        self,
        authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> ExecutionReconciliationRunResult:
        failures: list[str] = []
        unknown_result = UnknownResolutionRunResult(0, 0, 0, 0, 0, 0)
        generic_result = ExecutionReconciliationRunResult(0, 0, 0, 0)
        try:
            if authorization is None:
                unknown_result = await self.unknown.run_once()
            else:
                _require_reconciliation_effect(authorization)
                unknown_result = await self.unknown.run_scheduled(authorization)
        except Exception as exc:
            failures.append(f"unknown:{type(exc).__name__}")
        try:
            if authorization is None:
                generic_result = await self.generic.run_once()
            else:
                _require_reconciliation_effect(authorization)
                generic_result = await self.generic.run_scheduled(authorization)
        except Exception as exc:
            failures.append(f"generic:{type(exc).__name__}")
        if failures:
            raise ExecutionInvariantError(
                "reconciliation_stage_subrunner_failed:" + ",".join(failures)
            )
        return ExecutionReconciliationRunResult(
            claimed=(generic_result.claimed + unknown_result.claimed + unknown_result.resumed),
            completed=generic_result.completed + unknown_result.applied,
            rescheduled=generic_result.rescheduled,
            manual=generic_result.manual,
            failed=generic_result.failed + unknown_result.failed,
            unknown_listed=unknown_result.listed,
            unknown_claimed=unknown_result.claimed,
            unknown_resumed=unknown_result.resumed,
            unknown_applied=unknown_result.applied,
            unknown_replayed=unknown_result.replayed,
            unknown_failed=unknown_result.failed,
        )


def _resume_claim(candidate: UnknownResolutionCandidate) -> UnknownResolutionClaim:
    if candidate.claim_token is None or candidate.claim_expires_at is None:
        raise ExecutionInvariantError("unknown_resolution_owned_claim_is_incomplete")
    return UnknownResolutionClaim(
        command_id=candidate.command_id,
        request_id=candidate.request_id,
        review_id=candidate.review_id,
        break_id=candidate.break_id,
        intent_id=candidate.intent_id,
        terminal_status=candidate.terminal_status,
        request_payload_sha256=candidate.request_payload_sha256,
        review_payload_sha256=candidate.review_payload_sha256,
        command_revision=candidate.command_revision,
        work_revision=candidate.work_revision,
        claim_token=candidate.claim_token,
        claim_expires_at=candidate.claim_expires_at,
        expected_control_epoch=candidate.expected_control_epoch,
        account_id=candidate.account_id,
        environment=candidate.environment,
        holder_id=candidate.holder_id,
        release_sha=candidate.release_sha,
        lease_fencing_token=candidate.lease_fencing_token,
    )


def _require_reconciliation_effect(
    authorization: object,
) -> SchedulerInvocationEffectAuthorization:
    return require_scheduler_invocation_effect_authorization(
        authorization,
        expected_job_key="operations.reconciliation",
    )

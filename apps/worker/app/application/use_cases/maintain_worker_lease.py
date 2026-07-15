from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from app.application.ports.execution_kernel_port import WorkerLeasePort
from app.domain.common.time import now_utc
from app.domain.execution_v2.models import (
    ExecutionInvariantError,
    WorkerLease,
    WorkerLeaseRelease,
)


class MaintainWorkerLease:
    """Own exactly one account lease for the active V2 scheduler process."""

    def __init__(
        self,
        port: WorkerLeasePort,
        *,
        account_id: str,
        holder_id: str,
        ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if not account_id.strip() or not holder_id.strip():
            raise ExecutionInvariantError("worker_lease_identity_is_required")
        if ttl < timedelta(seconds=15):
            raise ExecutionInvariantError("worker_lease_ttl_is_too_short")
        self.port = port
        self.account_id = account_id
        self.holder_id = holder_id
        self.ttl = ttl
        self.clock = clock
        self.current: WorkerLease | None = None

    async def acquire(self) -> WorkerLease:
        if self.current is not None:
            raise ExecutionInvariantError("worker_lease_is_already_acquired")
        now = self._now()
        lease = await self.port.acquire_worker_lease(
            account_id=self.account_id,
            holder_id=self.holder_id,
            now=now,
            ttl=self.ttl,
        )
        self._validate_identity(lease)
        if not lease.is_active(now):
            raise ExecutionInvariantError("acquired_worker_lease_is_not_active")
        self.current = lease
        return lease

    async def renew(self) -> WorkerLease:
        current = self.current
        if current is None:
            raise ExecutionInvariantError("worker_lease_is_not_acquired")
        now = self._now()
        if not current.is_active(now):
            raise ExecutionInvariantError("worker_lease_expired_before_renewal")
        renewed = await self.port.renew_worker_lease(
            current,
            now=now,
            ttl=self.ttl,
        )
        self._validate_identity(renewed)
        if (
            renewed.fencing_token != current.fencing_token
            or renewed.acquired_at != current.acquired_at
            or renewed.expires_at <= current.expires_at
            or not renewed.is_active(now)
        ):
            raise ExecutionInvariantError("renewed_worker_lease_is_invalid")
        self.current = renewed
        return renewed

    async def release(self) -> WorkerLeaseRelease | None:
        current = self.current
        if current is None:
            return None
        released = await self.port.release_worker_lease(
            current,
            now=self._now(),
        )
        if (
            released.account_id != self.account_id
            or released.holder_id != self.holder_id
            or released.fencing_token != current.fencing_token
        ):
            raise ExecutionInvariantError("released_worker_lease_is_invalid")
        self.current = None
        return released

    def _validate_identity(self, lease: WorkerLease) -> None:
        if lease.account_id != self.account_id or lease.holder_id != self.holder_id:
            raise ExecutionInvariantError("worker_lease_identity_mismatch")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionInvariantError("worker_lease_clock_must_be_timezone_aware")
        return value

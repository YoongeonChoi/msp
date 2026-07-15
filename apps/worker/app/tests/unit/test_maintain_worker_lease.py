from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.application.use_cases.maintain_worker_lease import MaintainWorkerLease
from app.domain.execution_v2.models import (
    ExecutionInvariantError,
    WorkerLease,
    WorkerLeaseRelease,
)

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


async def test_manager_acquires_renews_and_releases_exact_fencing_identity() -> None:
    port = LeasePort()
    times = iter((NOW, NOW + timedelta(seconds=10), NOW + timedelta(seconds=20)))
    manager = MaintainWorkerLease(
        port,
        account_id="paper-primary",
        holder_id="worker-a",
        ttl=timedelta(seconds=30),
        clock=lambda: next(times),
    )

    acquired = await manager.acquire()
    renewed = await manager.renew()
    released = await manager.release()

    assert acquired.fencing_token == renewed.fencing_token == 7
    assert renewed.expires_at == NOW + timedelta(seconds=40)
    assert released is not None and released.fencing_token == 7
    assert manager.current is None
    assert port.calls == ["acquire", "renew", "release"]


async def test_manager_never_renews_an_expired_lease() -> None:
    manager = MaintainWorkerLease(
        LeasePort(),
        account_id="paper-primary",
        holder_id="worker-a",
        ttl=timedelta(seconds=30),
        clock=lambda: NOW + timedelta(seconds=31),
    )
    manager.current = WorkerLease(
        account_id="paper-primary",
        holder_id="worker-a",
        fencing_token=7,
        acquired_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(ExecutionInvariantError, match="expired_before_renewal"):
        await manager.renew()


class LeasePort:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def acquire_worker_lease(
        self,
        *,
        account_id: str,
        holder_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        self.calls.append("acquire")
        return WorkerLease(account_id, holder_id, 7, now, now + ttl)

    async def renew_worker_lease(
        self,
        lease: WorkerLease,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        self.calls.append("renew")
        return WorkerLease(
            lease.account_id,
            lease.holder_id,
            lease.fencing_token,
            lease.acquired_at,
            now + ttl,
        )

    async def release_worker_lease(
        self,
        lease: WorkerLease,
        *,
        now: datetime,
    ) -> WorkerLeaseRelease:
        self.calls.append("release")
        return WorkerLeaseRelease(
            lease.account_id,
            lease.holder_id,
            lease.fencing_token,
            now,
            False,
        )

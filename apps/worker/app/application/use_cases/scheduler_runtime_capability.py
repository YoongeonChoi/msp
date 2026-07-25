from __future__ import annotations

from typing import Protocol

from app.application.ports.durable_scheduler_port import DurableSchedulerPort
from app.application.ports.persistence_authority import PersistenceAuthority
from app.domain.execution_v2.models import WorkerLease


class SchedulerRuntimeCapability(Protocol):
    """Read-only application contract for one attested scheduler runtime."""

    @property
    def scheduler_port(self) -> DurableSchedulerPort: ...

    @property
    def account_id(self) -> str: ...

    @property
    def holder_id(self) -> str: ...

    @property
    def release_sha(self) -> str: ...

    @property
    def persistence_authority(self) -> PersistenceAuthority: ...

    def assert_intact(self) -> None: ...

    def current_outer_lease(self) -> WorkerLease: ...

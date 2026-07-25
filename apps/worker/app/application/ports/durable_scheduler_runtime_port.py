from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.application.use_cases.run_durable_scheduler import (
        DurableSchedulerConvergenceResult,
        DurableSchedulerRunResult,
    )


class DurableSchedulerRuntimePort(Protocol):
    """Opaque, attested facade used by the production scheduler loop.

    The application loop deliberately receives neither a binding registry nor
    individual operation handlers.  The facade owns those capabilities and
    exposes only the two serial lifecycle operations.
    """

    def assert_intact(self) -> None:
        """Fail closed when the sealed runtime graph no longer matches issuance."""

        ...

    async def converge_step(self) -> DurableSchedulerConvergenceResult:
        """Converge definitions and settle any authorized recovery invocation."""

        ...

    async def run_once(self) -> DurableSchedulerRunResult:
        """Claim and settle at most one normally due invocation."""

        ...


__all__ = ("DurableSchedulerRuntimePort",)

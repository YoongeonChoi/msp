from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from types import TracebackType

import httpx

from app.adapters.broker.toss_auth import TossAuth
from app.adapters.broker.toss_client import TossClient
from app.adapters.market_data.toss_market_data import TossMarketData
from app.adapters.persistence.supabase_calendar_observation_store import (
    SupabaseCalendarObservationStore,
)
from app.adapters.persistence.supabase_kr_calendar_collection_job_store import (
    SupabaseKrCalendarCollectionJobStore,
)
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KrCalendarCollectionRecoveryAssessmentService,
)
from app.application.use_cases.collect_kr_daily_session_observation import (
    CollectKrDailySessionObservation,
)
from app.application.use_cases.run_guarded_kr_calendar_collection_job_once import (
    RunGuardedKrCalendarCollectionJobOnce,
)
from app.application.use_cases.run_kr_calendar_date_range_collection_job import (
    RunKrCalendarDateRangeCollectionJob,
)
from app.config import Settings
from app.domain.common.errors import KnownFailClosedError


class KrCalendarCollectionRuntimeError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("kr_calendar_collection_runtime", safe_message)


@dataclass(slots=True)
class KrCalendarCollectionAssessmentRuntime:
    """Own the Worker-only client for one explicit read-only assessment."""

    assessment_service: KrCalendarCollectionRecoveryAssessmentService
    supabase_client: httpx.AsyncClient
    job_store: SupabaseKrCalendarCollectionJobStore
    _resources: AsyncExitStack = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._resources.aclose()

    async def __aenter__(self) -> KrCalendarCollectionAssessmentRuntime:
        if self._closed:
            raise KrCalendarCollectionRuntimeError(
                "kr_calendar_collection_assessment_runtime_already_closed"
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()


@dataclass(slots=True)
class KrCalendarCollectionRuntime:
    """Own the resources for one explicit calendar collection invocation."""

    runner: RunGuardedKrCalendarCollectionJobOnce
    toss_client: TossClient
    supabase_client: httpx.AsyncClient
    observation_store: SupabaseCalendarObservationStore
    job_store: SupabaseKrCalendarCollectionJobStore
    _resources: AsyncExitStack = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._resources.aclose()

    async def __aenter__(self) -> KrCalendarCollectionRuntime:
        if self._closed:
            raise KrCalendarCollectionRuntimeError(
                "kr_calendar_collection_runtime_already_closed"
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()


async def build_kr_calendar_collection_assessment_runtime(
    settings: Settings,
) -> KrCalendarCollectionAssessmentRuntime:
    """Build one read-only assessment boundary without Toss/provider clients."""

    if type(settings) is not Settings:
        raise KrCalendarCollectionRuntimeError(
            "kr_calendar_collection_assessment_runtime_settings_invalid"
        )
    try:
        settings.require_kr_calendar_collection_assessment()
    except ValueError:
        raise KrCalendarCollectionRuntimeError(
            "kr_calendar_collection_assessment_runtime_configuration_invalid"
        ) from None

    async with AsyncExitStack() as construction:
        supabase_client = await construction.enter_async_context(
            httpx.AsyncClient(timeout=10.0)
        )
        job_store = SupabaseKrCalendarCollectionJobStore(
            settings,
            client=supabase_client,
        )
        construction.push_async_callback(job_store.close)
        assessment_service = KrCalendarCollectionRecoveryAssessmentService(
            job_store
        )
        owned_resources = construction.pop_all()

    return KrCalendarCollectionAssessmentRuntime(
        assessment_service=assessment_service,
        supabase_client=supabase_client,
        job_store=job_store,
        _resources=owned_resources,
    )


async def build_kr_calendar_collection_runtime(
    settings: Settings,
) -> KrCalendarCollectionRuntime:
    """Build only the guarded manual runtime; never attach it to the worker loop."""

    if type(settings) is not Settings:
        raise KrCalendarCollectionRuntimeError(
            "kr_calendar_collection_runtime_settings_invalid"
        )
    try:
        settings.require_kr_calendar_collection_manual_execution()
    except ValueError:
        raise KrCalendarCollectionRuntimeError(
            "kr_calendar_collection_runtime_configuration_invalid"
        ) from None

    holder_id = settings.kr_calendar_collection_holder_id
    if type(holder_id) is not str:
        raise KrCalendarCollectionRuntimeError(
            "kr_calendar_collection_runtime_configuration_invalid"
        )

    async with AsyncExitStack() as construction:
        supabase_client = await construction.enter_async_context(
            httpx.AsyncClient(timeout=10.0)
        )
        toss_auth_client = await construction.enter_async_context(
            httpx.AsyncClient(timeout=10.0)
        )
        toss_read_client = await construction.enter_async_context(
            httpx.AsyncClient(timeout=10.0)
        )

        toss_auth = TossAuth(settings, client=toss_auth_client)
        toss_client = TossClient(
            settings,
            auth=toss_auth,
            client=toss_read_client,
        )
        construction.push_async_callback(toss_client.aclose)
        if (
            toss_client.execution_environment != "production_read_only"
            or toss_client.production_order_capable is not False
        ):
            raise KrCalendarCollectionRuntimeError(
                "kr_calendar_collection_runtime_read_only_adapter_required"
            )

        market_data = TossMarketData(toss_client)
        observation_store = SupabaseCalendarObservationStore(
            settings,
            client=supabase_client,
        )
        construction.push_async_callback(observation_store.close)
        collector = CollectKrDailySessionObservation(
            market_data,
            observation_store,
        )
        job_store = SupabaseKrCalendarCollectionJobStore(
            settings,
            client=supabase_client,
        )
        construction.push_async_callback(job_store.close)
        recovery_assessment = KrCalendarCollectionRecoveryAssessmentService(
            job_store
        )
        date_runner = RunKrCalendarDateRangeCollectionJob(
            collector,
            job_store,
            holder_id=holder_id,
            manual_execution_enabled=True,
        )
        runner = RunGuardedKrCalendarCollectionJobOnce(
            recovery_assessment,
            date_runner,
            manual_execution_enabled=True,
        )
        owned_resources = construction.pop_all()

    return KrCalendarCollectionRuntime(
        runner=runner,
        toss_client=toss_client,
        supabase_client=supabase_client,
        observation_store=observation_store,
        job_store=job_store,
        _resources=owned_resources,
    )

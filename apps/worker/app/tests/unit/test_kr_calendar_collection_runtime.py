from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

import app.kr_calendar_collection_runtime as runtime_module
from app.adapters.broker.toss_client import TossClient
from app.adapters.market_data.toss_market_data import TossMarketData
from app.adapters.persistence.supabase_calendar_observation_store import (
    SupabaseCalendarObservationStore,
)
from app.adapters.persistence.supabase_kr_calendar_collection_job_store import (
    SupabaseKrCalendarCollectionJobStore,
)
from app.application.use_cases.collect_kr_daily_session_observation import (
    CollectKrDailySessionObservation,
)
from app.config import Settings
from app.kr_calendar_collection_runtime import (
    KrCalendarCollectionAssessmentRuntime,
    KrCalendarCollectionRuntimeError,
    build_kr_calendar_collection_assessment_runtime,
    build_kr_calendar_collection_runtime,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED": True,
        "KR_CALENDAR_COLLECTION_MANUAL_EXECUTION_ENABLED": True,
        "KR_CALENDAR_COLLECTION_HOLDER_ID": (
            "00000000-0000-4000-8000-000000000099"
        ),
        "MOCK_PROVIDERS": False,
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": "dummy-test-token",
        "TOSS_CLIENT_ID": "dummy-client-id",
        "TOSS_CLIENT_SECRET": "dummy-client-secret",
        "TOSS_CREDENTIAL_SCOPE": "read_only",
        "TOSS_ORDER_CAPABLE_CREDENTIALS": False,
    }
    return Settings.model_validate(values | overrides)


def _assessment_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED": True,
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": "dummy-test-token",
    }
    return Settings.model_validate(values | overrides)


async def test_assessment_factory_wires_only_read_only_supabase_inspection() -> None:
    runtime = await build_kr_calendar_collection_assessment_runtime(
        _assessment_settings()
    )
    client = runtime.supabase_client
    try:
        assert type(runtime) is KrCalendarCollectionAssessmentRuntime
        assert type(runtime.job_store) is SupabaseKrCalendarCollectionJobStore
        assert runtime.assessment_service.inspector is runtime.job_store
        assert runtime.job_store.client is client
        assert runtime.job_store._owns_client is False
        assert not client.is_closed
    finally:
        await runtime.close()

    assert client.is_closed
    await runtime.close()


async def test_assessment_factory_rejects_disabled_config_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_creations = 0

    def unexpected_client(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        nonlocal client_creations
        client_creations += 1
        raise AssertionError("client construction must not occur")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)

    with pytest.raises(
        KrCalendarCollectionRuntimeError,
        match="kr_calendar_collection_assessment_runtime_configuration_invalid",
    ):
        await build_kr_calendar_collection_assessment_runtime(Settings())

    assert client_creations == 0


async def test_factory_wires_only_durable_read_only_calendar_runtime() -> None:
    runtime = await build_kr_calendar_collection_runtime(_settings())
    toss_read_client = runtime.toss_client.client
    toss_auth_client = runtime.toss_client.auth.client
    supabase_client = runtime.supabase_client
    try:
        guarded = runtime.runner
        date_runner = guarded.runner
        collector = date_runner.collector

        assert type(runtime.toss_client) is TossClient
        assert runtime.toss_client.production_order_capable is False
        assert runtime.toss_client.execution_environment == "production_read_only"
        assert type(collector) is CollectKrDailySessionObservation
        assert type(collector.source) is TossMarketData
        assert collector.source.toss is runtime.toss_client
        assert type(runtime.observation_store) is SupabaseCalendarObservationStore
        assert collector.store is runtime.observation_store
        assert type(runtime.job_store) is SupabaseKrCalendarCollectionJobStore
        assert date_runner.job_store is runtime.job_store
        assert guarded.recovery_assessment_service.inspector is runtime.job_store
        assert runtime.observation_store.client is supabase_client
        assert runtime.job_store.client is supabase_client
        assert runtime.observation_store._owns_client is False
        assert runtime.job_store._owns_client is False
        assert date_runner.persistence_kind == "durable"
        assert date_runner.manual_execution_enabled is True
        assert guarded.manual_execution_enabled is True
        assert not supabase_client.is_closed
        assert not toss_read_client.is_closed
        assert not toss_auth_client.is_closed
    finally:
        await runtime.close()

    assert supabase_client.is_closed
    assert toss_read_client.is_closed
    assert toss_auth_client.is_closed
    await runtime.close()


async def test_factory_rejects_disabled_configuration_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_creations = 0

    def unexpected_client(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        nonlocal client_creations
        client_creations += 1
        raise AssertionError("client construction must not occur")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)

    with pytest.raises(
        KrCalendarCollectionRuntimeError,
        match="kr_calendar_collection_runtime_configuration_invalid",
    ):
        await build_kr_calendar_collection_runtime(Settings())

    assert client_creations == 0


async def test_factory_closes_every_owned_client_after_partial_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def tracked_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client = real_client(*args, **kwargs)
        clients.append(client)
        return client

    def fail_guarded_runner(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("sensitive-construction-detail")

    monkeypatch.setattr(httpx, "AsyncClient", tracked_client)
    monkeypatch.setattr(
        runtime_module,
        "RunGuardedKrCalendarCollectionJobOnce",
        fail_guarded_runner,
    )

    with pytest.raises(RuntimeError, match="sensitive-construction-detail"):
        await build_kr_calendar_collection_runtime(_settings())

    assert len(clients) == 3
    assert all(client.is_closed for client in clients)


async def test_runtime_context_closes_resources_when_cancelled() -> None:
    runtime = await build_kr_calendar_collection_runtime(_settings())
    toss_read_client = runtime.toss_client.client
    toss_auth_client = runtime.toss_client.auth.client
    supabase_client = runtime.supabase_client

    with pytest.raises(asyncio.CancelledError):
        async with runtime:
            raise asyncio.CancelledError

    assert supabase_client.is_closed
    assert toss_read_client.is_closed
    assert toss_auth_client.is_closed

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.in_memory_candle_observation_store import (
    InMemoryCandleObservationStore,
)
from app.adapters.persistence.in_memory_daily_candle_collection_job_store import (
    InMemoryDailyCandleCollectionJobStore,
)
from app.adapters.persistence.supabase_candle_observation_store import (
    SupabaseCandleObservationStore,
)
from app.adapters.persistence.supabase_daily_candle_collection_job_store import (
    SupabaseDailyCandleCollectionJobStore,
)
from app.application.ports.candle_observation_store_port import (
    CandleObservationStorePort,
)
from app.application.ports.daily_candle_collection_job_store_port import (
    DailyCandleCollectionJobStorePort,
)
from app.application.ports.persistence_authority import (
    is_persistence_authority,
    persistence_authority_fingerprint,
)
from app.config import Settings


def test_fingerprint_canonicalizes_equivalent_origins() -> None:
    first = persistence_authority_fingerprint(
        namespace="supabase-worker-api",
        origin="HTTPS://Project.SUPABASE.CO:443/",
        profile="worker_api",
    )
    second = persistence_authority_fingerprint(
        namespace="supabase-worker-api",
        origin="https://project.supabase.co",
        profile="worker_api",
    )

    assert first == second
    assert is_persistence_authority(first)


def test_fingerprint_binds_origin_and_profile_without_exposing_them() -> None:
    origin = "https://project-a.supabase.co"
    first = persistence_authority_fingerprint(
        namespace="supabase-worker-api",
        origin=origin,
        profile="worker_api",
    )
    other_origin = persistence_authority_fingerprint(
        namespace="supabase-worker-api",
        origin="https://project-b.supabase.co",
        profile="worker_api",
    )
    other_profile = persistence_authority_fingerprint(
        namespace="supabase-worker-api",
        origin=origin,
        profile="audit_api",
    )

    assert first != other_origin
    assert first != other_profile
    assert origin not in first
    assert "worker_api" not in first


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@project.supabase.co",
        "https://project.supabase.co/rest/v1",
        "https://project.supabase.co?token=secret",
        "https://project.supabase.co#secret",
        "https://project.supabase.co:secret",
    ],
)
def test_fingerprint_rejects_non_origin_or_credential_bearing_url(origin: str) -> None:
    with pytest.raises(
        ValueError,
        match="persistence_authority_origin_invalid",
    ) as captured:
        persistence_authority_fingerprint(
            namespace="supabase-worker-api",
            origin=origin,
            profile="worker_api",
        )

    assert origin not in str(captured.value)
    assert captured.value.__context__ is None


def test_reference_stores_satisfy_shared_authority_contract() -> None:
    observation_store: CandleObservationStorePort = InMemoryCandleObservationStore()
    job_store: DailyCandleCollectionJobStorePort = InMemoryDailyCandleCollectionJobStore()

    assert observation_store.persistence_kind == "reference"
    assert job_store.persistence_kind == "reference"
    assert observation_store.persistence_authority == job_store.persistence_authority
    assert is_persistence_authority(observation_store.persistence_authority)


async def test_supabase_stores_bind_same_origin_independent_of_secret() -> None:
    first_origin = "http://127.0.0.1:54321"
    other_origin = "http://127.0.0.1:54322"
    first_secret = "first-test-secret"
    second_secret = "second-test-secret"
    async with httpx.AsyncClient() as client:
        observation_store: CandleObservationStorePort = SupabaseCandleObservationStore(
            _settings(first_origin, first_secret),
            client=client,
        )
        job_store: DailyCandleCollectionJobStorePort = SupabaseDailyCandleCollectionJobStore(
            _settings(first_origin, second_secret),
            client=client,
        )
        other_observation_store = SupabaseCandleObservationStore(
            _settings(other_origin, first_secret),
            client=client,
        )

    assert observation_store.persistence_kind == "durable"
    assert job_store.persistence_kind == "durable"
    assert observation_store.persistence_authority == job_store.persistence_authority
    assert observation_store.persistence_authority != (
        other_observation_store.persistence_authority
    )
    assert first_secret not in observation_store.persistence_authority
    assert second_secret not in observation_store.persistence_authority
    assert first_origin not in observation_store.persistence_authority
    assert is_persistence_authority(observation_store.persistence_authority)


def _settings(origin: str, secret: str) -> Settings:
    return Settings.model_validate(
        {
            "SUPABASE_URL": origin,
            "SUPABASE_SECRET_KEY": SecretStr(secret),
        }
    )

from __future__ import annotations

from typing import Protocol

import httpx
from pydantic import SecretStr

from app.adapters.persistence.backtest_repository import SupabaseBacktestRepository
from app.adapters.persistence.outcome_tracking_repository import (
    SupabaseOutcomeTrackingRepository,
)
from app.adapters.persistence.paper_health_repository import SupabasePaperHealthRepository
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.config import Settings
from app.infrastructure.supabase_headers import supabase_api_headers
from app.tools.redeploy_render_worker import begin_worker_deployment
from app.tools.verify_worker_release_freshness import fetch_latest_worker_heartbeat

NEW_SECRET_KEY = "sb_secret_test_only"
NEW_PUBLISHABLE_KEY = "sb_publishable_test_only"
LEGACY_SERVICE_ROLE_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
NON_PREFIX_TEST_KEY = "service-secret"
TARGET_SHA = "4ad9c7599b7b112bf30763b9e37dad944f60997b"


class RepositoryWithHeaders(Protocol):
    headers: dict[str, str]

    async def aclose(self) -> None: ...


def test_new_secret_key_is_only_sent_as_apikey() -> None:
    assert supabase_api_headers(NEW_SECRET_KEY) == {"apikey": NEW_SECRET_KEY}


def test_new_publishable_key_is_only_sent_as_apikey() -> None:
    assert supabase_api_headers(NEW_PUBLISHABLE_KEY) == {"apikey": NEW_PUBLISHABLE_KEY}


def test_legacy_service_role_jwt_keeps_bearer_authorization() -> None:
    assert supabase_api_headers(LEGACY_SERVICE_ROLE_JWT) == {
        "apikey": LEGACY_SERVICE_ROLE_JWT,
        "authorization": "Bearer " + LEGACY_SERVICE_ROLE_JWT,
    }


def test_non_prefix_key_keeps_legacy_bearer_behavior() -> None:
    assert supabase_api_headers(NON_PREFIX_TEST_KEY) == {
        "apikey": NON_PREFIX_TEST_KEY,
        "authorization": "Bearer " + NON_PREFIX_TEST_KEY,
    }


async def test_supabase_repositories_omit_bearer_for_new_secret_key() -> None:
    settings = Settings(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr(NEW_SECRET_KEY),
    )
    repositories: list[RepositoryWithHeaders] = [
        SupabaseRepository(settings),
        SupabasePaperHealthRepository(settings),
        SupabaseOutcomeTrackingRepository(settings),
        SupabaseBacktestRepository(settings),
    ]
    try:
        for repository in repositories:
            assert repository.headers["apikey"] == NEW_SECRET_KEY
            assert "authorization" not in repository.headers
    finally:
        for repository in repositories:
            await repository.aclose()


def test_worker_release_fetch_omits_bearer_for_new_secret_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == NEW_SECRET_KEY
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        row = fetch_latest_worker_heartbeat(
            supabase_url="https://example.supabase.co",
            supabase_secret_key=NEW_SECRET_KEY,
            client=client,
        )

    assert row is None


def test_deployment_rpc_omits_bearer_for_new_secret_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == NEW_SECRET_KEY
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={"deployment_lock": True, "target_sha_short": TARGET_SHA[:12]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        begin_worker_deployment(
            supabase_url="https://example.supabase.co",
            supabase_secret_key=NEW_SECRET_KEY,
            target_sha=TARGET_SHA,
            client=client,
        )

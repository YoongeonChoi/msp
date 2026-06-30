from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

import app.tools.redeploy_render_worker as redeploy
from app.domain.common.json import JsonObject
from app.tools.trigger_render_deploy_hook import RENDER_DEPLOY_HOOK_ENV

EXPECTED_SHA = "4ad9c7599b7b112bf30763b9e37dad944f60997b"
OTHER_SHA = "976ced92783109527decb1675d17d9b1526f2d52"
HOOK_URL = "https://api.render.com/deploy/srv-test?key=super-secret-token"
NOW = datetime(2026, 7, 1, 3, 0, 0, tzinfo=UTC)


def test_redeploy_posts_hook_and_waits_for_matching_freshness() -> None:
    seen_requests: list[httpx.Request] = []
    heartbeat_rows = [
        _heartbeat_row(OTHER_SHA, NOW - timedelta(seconds=30)),
        _heartbeat_row(EXPECTED_SHA, NOW - timedelta(seconds=2)),
    ]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/deploy/srv-test"
        assert request.url.params["key"] == "super-secret-token"
        assert request.url.params["ref"] == EXPECTED_SHA
        return httpx.Response(200)

    def fetcher(**_kwargs: object) -> JsonObject | None:
        return heartbeat_rows.pop(0)

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA.upper(),
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(transport=httpx.MockTransport(handler)),
        heartbeat_fetcher=fetcher,
        now_fn=lambda: NOW,
        sleep_fn=sleeps.append,
        monotonic_fn=_monotonic_counter(),
        poll_interval_sec=0.5,
        poll_timeout_sec=5.0,
    )

    assert len(seen_requests) == 1
    assert summary.result == "PASS"
    assert summary.reason is None
    assert summary.expected_sha_short == EXPECTED_SHA[:12]
    assert summary.observed_sha_short == EXPECTED_SHA[:12]
    assert summary.heartbeat_age_sec == 2
    assert summary.deploy_status_code == 200
    assert summary.attempts == 2
    assert sleeps == [0.5]
    assert redeploy._format_summary(summary) == (
        "FINAL=PASS render_worker_redeploy "
        "deploy_status_code=200 "
        f"expected_sha_short={EXPECTED_SHA[:12]} "
        f"observed_sha_short={EXPECTED_SHA[:12]} "
        "heartbeat_age_sec=2 max_age_sec=300 attempts=2"
    )


def test_redeploy_times_out_without_printing_full_sha_or_hook_secret() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    def fetcher(**_kwargs: object) -> JsonObject:
        return _heartbeat_row(OTHER_SHA, NOW - timedelta(seconds=30))

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(transport=httpx.MockTransport(handler)),
        heartbeat_fetcher=fetcher,
        now_fn=lambda: NOW,
        sleep_fn=lambda _seconds: None,
        monotonic_fn=_monotonic_counter(step=10.0),
        poll_interval_sec=1.0,
        poll_timeout_sec=1.0,
    )
    output = redeploy._format_summary(summary)

    assert summary.result == "FAIL"
    assert summary.reason == "freshness_timeout,release_sha_mismatch"
    assert f"expected_sha_short={EXPECTED_SHA[:12]}" in output
    assert f"observed_sha_short={OTHER_SHA[:12]}" in output
    assert EXPECTED_SHA not in output
    assert OTHER_SHA not in output
    assert "super-secret-token" not in output
    assert "api.render.com" not in output


def test_cli_skips_without_hook_url(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = redeploy.main(["--expected-sha", EXPECTED_SHA], environ={})

    output = capsys.readouterr().out
    assert exit_code == 2
    assert output.strip() == (
        "FINAL=SKIP render_worker_redeploy "
        "reason=render_deploy_hook_env_missing missing=RENDER_DEPLOY_HOOK_URL "
        f"expected_sha_short={EXPECTED_SHA[:12]}"
    )


def test_cli_requires_confirmation_before_network_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    requested = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200)

    exit_code = redeploy.main(
        ["--expected-sha", EXPECTED_SHA],
        environ={RENDER_DEPLOY_HOOK_ENV: HOOK_URL},
        hook_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert requested is False
    assert output.strip() == (
        "FINAL=SKIP render_worker_redeploy "
        f"reason=confirmation_required expected_sha_short={EXPECTED_SHA[:12]}"
    )
    assert "super-secret-token" not in output
    assert "api.render.com" not in output


def test_cli_requires_hosted_supabase_env_before_network_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requested = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200)

    monkeypatch.setattr(
        redeploy,
        "load_settings",
        lambda: _FakeSettings(supabase_url=None, supabase_secret_key=None),
    )

    exit_code = redeploy.main(
        ["--expected-sha", EXPECTED_SHA, "--yes"],
        environ={RENDER_DEPLOY_HOOK_ENV: HOOK_URL},
        hook_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert requested is False
    assert (
        "FINAL=SKIP render_worker_redeploy "
        "reason=hosted_supabase_env_missing missing=SUPABASE_URL,SUPABASE_SECRET_KEY "
        f"expected_sha_short={EXPECTED_SHA[:12]}"
    ) in output


def test_cli_prints_hook_pass_then_final_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    def fetcher(**_kwargs: object) -> JsonObject:
        return _heartbeat_row(EXPECTED_SHA, NOW - timedelta(seconds=1))

    monkeypatch.setattr(
        redeploy,
        "load_settings",
        lambda: _FakeSettings(
            supabase_url="https://project.supabase.co",
            supabase_secret_key=SecretStr("service-secret"),
        ),
    )

    exit_code = redeploy.main(
        [
            "--expected-sha",
            EXPECTED_SHA,
            "--yes",
            "--poll-timeout-sec",
            "0",
        ],
        environ={RENDER_DEPLOY_HOOK_ENV: HOOK_URL},
        hook_client=httpx.Client(transport=httpx.MockTransport(handler)),
        heartbeat_fetcher=fetcher,
        now_fn=lambda: NOW,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS render_deploy_hook "
        f"expected_sha_short={EXPECTED_SHA[:12]} status_code=200"
    ) in output
    assert "FINAL=PASS render_worker_redeploy" in output
    assert "super-secret-token" not in output
    assert "api.render.com" not in output


class _FakeSettings:
    def __init__(
        self,
        *,
        supabase_url: str | None,
        supabase_secret_key: SecretStr | None,
    ) -> None:
        self.supabase_url = supabase_url
        self.supabase_secret_key = supabase_secret_key


def _heartbeat_row(release_sha: str, created_at: datetime) -> JsonObject:
    return {
        "status": "ok",
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "details": {"release_sha": release_sha},
    }


def _monotonic_counter(step: float = 0.1) -> Callable[[], float]:
    value = 0.0

    def monotonic() -> float:
        nonlocal value
        current = value
        value += step
        return current

    return monotonic

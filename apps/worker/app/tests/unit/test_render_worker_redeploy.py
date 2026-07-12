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
    events: list[str] = []
    heartbeat_rows = [
        _heartbeat_row(
            OTHER_SHA,
            NOW - timedelta(seconds=3),
            deployment_lock=True,
            deployment_target_sha=EXPECTED_SHA,
        ),
        _heartbeat_row(EXPECTED_SHA, NOW - timedelta(seconds=3)),
        _heartbeat_row(EXPECTED_SHA, NOW - timedelta(seconds=1)),
    ]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("hook")
        seen_requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/deploy/srv-test"
        assert request.url.params["key"] == "super-secret-token"
        assert request.url.params["ref"] == EXPECTED_SHA
        return httpx.Response(200)

    def fetcher(**_kwargs: object) -> JsonObject | None:
        events.append("heartbeat")
        return heartbeat_rows.pop(0)

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA.upper(),
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(transport=httpx.MockTransport(handler)),
        deployment_client=_deployment_client(events),
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
    assert summary.heartbeat_age_sec == 1
    assert summary.deploy_status_code == 200
    assert summary.attempts == 2
    assert summary.pause_attempts == 1
    assert sleeps == [0.5]
    assert events == [
        "lock",
        "heartbeat",
        "hook",
        "triggered",
        "heartbeat",
        "heartbeat",
        "unlock",
    ]
    assert redeploy._format_summary(summary) == (
        "FINAL=PASS render_worker_redeploy "
        "deploy_status_code=200 "
        f"expected_sha_short={EXPECTED_SHA[:12]} "
        f"observed_sha_short={EXPECTED_SHA[:12]} "
        "heartbeat_age_sec=1 max_age_sec=300 attempts=2 pause_attempts=1 "
        "deployment_locked=1 deployment_unlocked=1"
    )


def test_redeploy_times_out_without_printing_full_sha_or_hook_secret() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    heartbeat_rows = [
        _heartbeat_row(
            OTHER_SHA,
            NOW - timedelta(seconds=2),
            deployment_lock=True,
            deployment_target_sha=EXPECTED_SHA,
        ),
        _heartbeat_row(OTHER_SHA, NOW - timedelta(seconds=1)),
    ]

    def fetcher(**_kwargs: object) -> JsonObject:
        if heartbeat_rows:
            return heartbeat_rows.pop(0)
        return _heartbeat_row(OTHER_SHA, NOW - timedelta(seconds=1))

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(transport=httpx.MockTransport(handler)),
        deployment_client=_deployment_client(),
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
    assert summary.deployment_locked is True
    assert summary.deployment_unlocked is False


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("hook_timeout_sec", float("nan"), "hook_timeout_sec_must_be_positive"),
        ("poll_interval_sec", float("inf"), "poll_interval_sec_must_be_positive"),
        ("poll_timeout_sec", float("nan"), "poll_timeout_sec_must_be_non_negative"),
        ("pause_timeout_sec", float("inf"), "pause_timeout_sec_must_be_non_negative"),
    ],
)
def test_redeploy_rejects_non_finite_timeouts(
    field: str,
    value: float,
    expected_error: str,
) -> None:
    with pytest.raises(redeploy.RenderWorkerRedeployError, match=expected_error):
        if field == "hook_timeout_sec":
            redeploy.redeploy_render_worker(
                hook_url=HOOK_URL,
                expected_sha=EXPECTED_SHA,
                supabase_url="https://project.supabase.co",
                supabase_secret_key="service-secret",
                hook_timeout_sec=value,
            )
        elif field == "poll_interval_sec":
            redeploy.redeploy_render_worker(
                hook_url=HOOK_URL,
                expected_sha=EXPECTED_SHA,
                supabase_url="https://project.supabase.co",
                supabase_secret_key="service-secret",
                poll_interval_sec=value,
            )
        elif field == "poll_timeout_sec":
            redeploy.redeploy_render_worker(
                hook_url=HOOK_URL,
                expected_sha=EXPECTED_SHA,
                supabase_url="https://project.supabase.co",
                supabase_secret_key="service-secret",
                poll_timeout_sec=value,
            )
        else:
            redeploy.redeploy_render_worker(
                hook_url=HOOK_URL,
                expected_sha=EXPECTED_SHA,
                supabase_url="https://project.supabase.co",
                supabase_secret_key="service-secret",
                pause_timeout_sec=value,
            )


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
        return _heartbeat_row(
            EXPECTED_SHA,
            NOW - timedelta(seconds=1),
            deployment_lock=True,
            deployment_target_sha=EXPECTED_SHA,
        )

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
        deployment_client=_deployment_client(),
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


def test_redeploy_does_not_call_hook_when_deployment_lock_fails() -> None:
    hook_called = False

    def hook_handler(request: httpx.Request) -> httpx.Response:
        nonlocal hook_called
        hook_called = True
        return httpx.Response(200, request=request)

    def lock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(transport=httpx.MockTransport(hook_handler)),
        deployment_client=httpx.Client(transport=httpx.MockTransport(lock_handler)),
        poll_timeout_sec=0,
    )

    assert summary.result == "FAIL"
    assert summary.reason == "begin_worker_deployment_failed"
    assert summary.deployment_locked is False
    assert hook_called is False


def test_redeploy_does_not_call_hook_until_worker_observes_pause() -> None:
    hook_called = False

    def hook_handler(request: httpx.Request) -> httpx.Response:
        nonlocal hook_called
        hook_called = True
        return httpx.Response(200, request=request)

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(transport=httpx.MockTransport(hook_handler)),
        deployment_client=_deployment_client(),
        heartbeat_fetcher=lambda **_kwargs: _heartbeat_row(
            OTHER_SHA,
            NOW - timedelta(seconds=1),
        ),
        now_fn=lambda: NOW,
        pause_timeout_sec=0,
    )

    assert summary.result == "FAIL"
    assert summary.reason == "deployment_pause_timeout,deployment_lock_not_observed"
    assert summary.pause_attempts == 1
    assert summary.deployment_locked is True
    assert summary.deployment_unlocked is False
    assert hook_called is False


def test_redeploy_keeps_lock_when_trigger_marker_fails() -> None:
    def deployment_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/begin_worker_deployment"):
            return httpx.Response(
                200,
                json={"deployment_lock": True, "target_sha_short": EXPECTED_SHA[:12]},
                request=request,
            )
        return httpx.Response(500, request=request)

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        deployment_client=httpx.Client(
            transport=httpx.MockTransport(deployment_handler)
        ),
        heartbeat_fetcher=lambda **_kwargs: _heartbeat_row(
            OTHER_SHA,
            NOW - timedelta(seconds=1),
            deployment_lock=True,
            deployment_target_sha=EXPECTED_SHA,
        ),
        now_fn=lambda: NOW,
        pause_timeout_sec=0,
    )

    assert summary.result == "FAIL"
    assert summary.reason == "mark_worker_deployment_triggered_failed"
    assert summary.deploy_status_code == 200
    assert summary.deployment_locked is True
    assert summary.deployment_unlocked is False


def test_redeploy_keeps_lock_when_database_refuses_unlock() -> None:
    def deployment_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/begin_worker_deployment"):
            return httpx.Response(
                200,
                json={"deployment_lock": True, "target_sha_short": EXPECTED_SHA[:12]},
                request=request,
            )
        if request.url.path.endswith("/mark_worker_deployment_triggered"):
            return httpx.Response(
                200,
                json={
                    "deployment_lock": True,
                    "target_sha_short": EXPECTED_SHA[:12],
                    "deployment_triggered": True,
                    "deployment_triggered_at": (
                        NOW - timedelta(seconds=2)
                    ).isoformat(),
                },
                request=request,
            )
        return httpx.Response(400, request=request)

    summary = redeploy.redeploy_render_worker(
        hook_url=HOOK_URL,
        expected_sha=EXPECTED_SHA,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        hook_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        ),
        deployment_client=httpx.Client(transport=httpx.MockTransport(deployment_handler)),
        heartbeat_fetcher=lambda **_kwargs: _heartbeat_row(
            EXPECTED_SHA,
            NOW - timedelta(seconds=1),
            deployment_lock=True,
            deployment_target_sha=EXPECTED_SHA,
        ),
        now_fn=lambda: NOW,
        poll_timeout_sec=0,
    )

    assert summary.result == "FAIL"
    assert summary.reason == "deployment_unlock_failed"
    assert summary.deployment_locked is True
    assert summary.deployment_unlocked is False


class _FakeSettings:
    def __init__(
        self,
        *,
        supabase_url: str | None,
        supabase_secret_key: SecretStr | None,
    ) -> None:
        self.supabase_url = supabase_url
        self.supabase_secret_key = supabase_secret_key


def _heartbeat_row(
    release_sha: str,
    created_at: datetime,
    *,
    deployment_lock: bool = False,
    deployment_target_sha: str | None = None,
) -> JsonObject:
    details: JsonObject = {
        "release_sha": release_sha,
        "deployment_lock": deployment_lock,
    }
    if deployment_target_sha is not None:
        details["deployment_target_sha"] = deployment_target_sha
    return {
        "status": "ok",
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "details": details,
    }


def _monotonic_counter(step: float = 0.1) -> Callable[[], float]:
    value = 0.0

    def monotonic() -> float:
        nonlocal value
        current = value
        value += step
        return current

    return monotonic


def _deployment_client(events: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/begin_worker_deployment"):
            if events is not None:
                events.append("lock")
            return httpx.Response(
                200,
                json={"deployment_lock": True, "target_sha_short": EXPECTED_SHA[:12]},
                request=request,
            )
        if request.url.path.endswith("/mark_worker_deployment_triggered"):
            if events is not None:
                events.append("triggered")
            return httpx.Response(
                200,
                json={
                    "deployment_lock": True,
                    "target_sha_short": EXPECTED_SHA[:12],
                    "deployment_triggered": True,
                    "deployment_triggered_at": (
                        NOW - timedelta(seconds=2)
                    ).isoformat(),
                },
                request=request,
            )
        if request.url.path.endswith("/complete_worker_deployment"):
            if events is not None:
                events.append("unlock")
            return httpx.Response(
                200,
                json={"deployment_lock": False, "target_sha_short": EXPECTED_SHA[:12]},
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))

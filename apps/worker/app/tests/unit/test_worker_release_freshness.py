from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

import app.tools.verify_worker_release_freshness as freshness
from app.domain.common.json import JsonObject

EXPECTED_SHA = "2bac8362b50485537f82a6296ed969b8d73ebc0c"
OTHER_SHA = "976ced92783109527decb1675d17d9b1526f2d52"
NOW = datetime(2026, 6, 30, 14, 0, 0, tzinfo=UTC)


def test_worker_release_freshness_passes_for_matching_fresh_heartbeat() -> None:
    summary = freshness.verify_worker_release_freshness(
        _heartbeat_row(EXPECTED_SHA, NOW - timedelta(seconds=30)),
        expected_sha=EXPECTED_SHA.upper(),
        now=NOW,
    )

    assert summary.result == "PASS"
    assert summary.reason is None
    assert summary.expected_sha_short == EXPECTED_SHA[:12]
    assert summary.observed_sha_short == EXPECTED_SHA[:12]
    assert summary.heartbeat_age_sec == 30


def test_worker_release_freshness_fails_for_stale_mismatched_release() -> None:
    summary = freshness.verify_worker_release_freshness(
        _heartbeat_row(OTHER_SHA, NOW - timedelta(seconds=301)),
        expected_sha=EXPECTED_SHA,
        now=NOW,
        max_age_seconds=300,
    )

    assert summary.result == "FAIL"
    assert summary.reason == "release_sha_mismatch,heartbeat_stale"
    assert freshness._format_summary(summary) == (
        "FINAL=FAIL worker_release_freshness "
        "reason=release_sha_mismatch,heartbeat_stale "
        f"expected_sha_short={EXPECTED_SHA[:12]} "
        f"observed_sha_short={OTHER_SHA[:12]} "
        "heartbeat_age_sec=301 max_age_sec=300"
    )


def test_worker_release_freshness_fails_without_heartbeat() -> None:
    summary = freshness.verify_worker_release_freshness(
        None,
        expected_sha=EXPECTED_SHA,
        now=NOW,
    )

    assert summary.result == "FAIL"
    assert summary.reason == "heartbeat_missing"
    assert summary.observed_sha_short is None
    assert summary.heartbeat_age_sec is None


def test_worker_release_freshness_fails_for_future_heartbeat() -> None:
    summary = freshness.verify_worker_release_freshness(
        _heartbeat_row(EXPECTED_SHA, NOW + timedelta(seconds=1)),
        expected_sha=EXPECTED_SHA,
        now=NOW,
    )

    assert summary.result == "FAIL"
    assert summary.reason == "heartbeat_timestamp_invalid"
    assert summary.observed_sha_short == EXPECTED_SHA[:12]
    assert summary.heartbeat_age_sec is None


def test_worker_release_freshness_rejects_too_short_expected_sha() -> None:
    with pytest.raises(freshness.WorkerReleaseFreshnessError) as exc_info:
        freshness.verify_worker_release_freshness(
            _heartbeat_row(EXPECTED_SHA, NOW),
            expected_sha=EXPECTED_SHA[:7],
            now=NOW,
        )

    assert str(exc_info.value) == "expected_sha_invalid"


def test_fetch_latest_worker_heartbeat_uses_service_role_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/worker_heartbeats"
        assert request.url.query.decode("utf-8") == freshness.LATEST_HEARTBEAT_QUERY
        assert request.headers["apikey"] == "service-secret"
        assert request.headers["authorization"] == "Bearer service-secret"
        return httpx.Response(
            200,
            json=[_heartbeat_row(EXPECTED_SHA, NOW)],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    row = freshness.fetch_latest_worker_heartbeat(
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-secret",
        client=client,
    )

    assert row is not None
    assert row["details"] == {"release_sha": EXPECTED_SHA}


def test_cli_skips_when_hosted_supabase_env_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        freshness,
        "load_settings",
        lambda: _FakeSettings(supabase_url=None, supabase_secret_key=None),
    )

    exit_code = freshness.main(["--expected-sha", EXPECTED_SHA])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "FINAL=SKIP worker_release_freshness "
        "reason=hosted_supabase_env_missing missing=SUPABASE_URL,SUPABASE_SECRET_KEY"
    ) in output


def test_cli_reports_stale_mismatched_hosted_release(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        freshness,
        "load_settings",
        lambda: _FakeSettings(
            supabase_url="https://project.supabase.co",
            supabase_secret_key=SecretStr("service-secret"),
        ),
    )
    monkeypatch.setattr(
        freshness,
        "fetch_latest_worker_heartbeat",
        lambda **_kwargs: _heartbeat_row(
            OTHER_SHA, datetime.now(UTC) - timedelta(seconds=400)
        ),
    )

    exit_code = freshness.main(["--expected-sha", EXPECTED_SHA])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL worker_release_freshness" in output
    assert "reason=release_sha_mismatch,heartbeat_stale" in output
    assert f"expected_sha_short={EXPECTED_SHA[:12]}" in output
    assert f"observed_sha_short={OTHER_SHA[:12]}" in output
    assert EXPECTED_SHA not in output
    assert OTHER_SHA not in output


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

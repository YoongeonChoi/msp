from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx

from app.config import load_settings
from app.domain.common.json import JsonObject
from app.tools.trigger_render_deploy_hook import (
    RENDER_DEPLOY_HOOK_ENV,
    RenderDeployHookError,
    _git_head,
    _normalize_sha,
    _short_sha,
    trigger_render_deploy_hook,
)
from app.tools.verify_worker_release_freshness import (
    DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    WorkerReleaseFreshnessError,
    WorkerReleaseFreshnessSummary,
    fetch_latest_worker_heartbeat,
    verify_worker_release_freshness,
)

DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_POLL_TIMEOUT_SECONDS = 600.0


class RenderWorkerRedeployError(ValueError):
    pass


class HeartbeatFetcher(Protocol):
    def __call__(
        self,
        *,
        supabase_url: str,
        supabase_secret_key: str,
        client: httpx.Client | None = None,
    ) -> JsonObject | None:
        ...


@dataclass(frozen=True, slots=True)
class RenderWorkerRedeploySummary:
    result: Literal["PASS", "FAIL"]
    reason: str | None
    expected_sha_short: str
    observed_sha_short: str | None
    heartbeat_age_sec: int | None
    max_age_sec: int
    deploy_status_code: int | None
    attempts: int


def redeploy_render_worker(
    *,
    hook_url: str,
    expected_sha: str,
    supabase_url: str,
    supabase_secret_key: str,
    hook_client: httpx.Client | None = None,
    heartbeat_client: httpx.Client | None = None,
    heartbeat_fetcher: HeartbeatFetcher = fetch_latest_worker_heartbeat,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    hook_timeout_sec: float = 10.0,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout_sec: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    max_age_seconds: int = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
) -> RenderWorkerRedeploySummary:
    normalized_sha = _normalize_sha(expected_sha, "expected_sha")
    if poll_interval_sec <= 0:
        raise RenderWorkerRedeployError("poll_interval_sec_must_be_positive")
    if poll_timeout_sec < 0:
        raise RenderWorkerRedeployError("poll_timeout_sec_must_be_non_negative")
    if max_age_seconds <= 0:
        raise RenderWorkerRedeployError("max_age_seconds_must_be_positive")

    expected_sha_short = _short_sha(normalized_sha)
    try:
        deploy_result = trigger_render_deploy_hook(
            hook_url,
            expected_sha=normalized_sha,
            timeout_sec=hook_timeout_sec,
            client=hook_client,
        )
    except RenderDeployHookError as exc:
        return RenderWorkerRedeploySummary(
            result="FAIL",
            reason=str(exc),
            expected_sha_short=expected_sha_short,
            observed_sha_short=None,
            heartbeat_age_sec=None,
            max_age_sec=max_age_seconds,
            deploy_status_code=None,
            attempts=0,
        )

    deadline = monotonic_fn() + poll_timeout_sec
    attempts = 0
    last_summary: WorkerReleaseFreshnessSummary | None = None
    last_failure_reason: str | None = None

    while True:
        attempts += 1
        try:
            row = heartbeat_fetcher(
                supabase_url=supabase_url,
                supabase_secret_key=supabase_secret_key,
                client=heartbeat_client,
            )
            last_summary = verify_worker_release_freshness(
                row,
                expected_sha=normalized_sha,
                now=now_fn(),
                max_age_seconds=max_age_seconds,
            )
            last_failure_reason = last_summary.reason
            if last_summary.result == "PASS":
                return _summary_from_freshness(
                    result="PASS",
                    reason=None,
                    freshness=last_summary,
                    deploy_status_code=deploy_result.status_code,
                    attempts=attempts,
                )
        except (WorkerReleaseFreshnessError, httpx.HTTPError, ValueError):
            last_summary = None
            last_failure_reason = "freshness_verification_unavailable"

        now_monotonic = monotonic_fn()
        if poll_timeout_sec == 0 or now_monotonic >= deadline:
            break
        sleep_for = min(poll_interval_sec, max(0.0, deadline - now_monotonic))
        sleep_fn(sleep_for)

    timeout_reason = "freshness_timeout"
    if last_failure_reason:
        timeout_reason += "," + last_failure_reason
    if last_summary is not None:
        return _summary_from_freshness(
            result="FAIL",
            reason=timeout_reason,
            freshness=last_summary,
            deploy_status_code=deploy_result.status_code,
            attempts=attempts,
        )
    return RenderWorkerRedeploySummary(
        result="FAIL",
        reason=timeout_reason,
        expected_sha_short=expected_sha_short,
        observed_sha_short=None,
        heartbeat_age_sec=None,
        max_age_sec=max_age_seconds,
        deploy_status_code=deploy_result.status_code,
        attempts=attempts,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    hook_client: httpx.Client | None = None,
    heartbeat_client: httpx.Client | None = None,
    heartbeat_fetcher: HeartbeatFetcher = fetch_latest_worker_heartbeat,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> int:
    args = _parse_args(argv)
    env = environ if environ is not None else os.environ
    try:
        expected_sha = args.expected_sha or _git_head(args.repo_root)
        normalized_sha = _normalize_sha(expected_sha, "expected_sha")
    except RenderDeployHookError as exc:
        print(f"FINAL=FAIL render_worker_redeploy reason={exc}")
        return 1

    expected_sha_short = _short_sha(normalized_sha)
    hook_url = args.hook_url or env.get(RENDER_DEPLOY_HOOK_ENV)
    if not hook_url:
        print(
            "FINAL=SKIP render_worker_redeploy "
            f"reason=render_deploy_hook_env_missing missing={RENDER_DEPLOY_HOOK_ENV} "
            f"expected_sha_short={expected_sha_short}"
        )
        return 2
    if not args.yes:
        print(
            "FINAL=SKIP render_worker_redeploy "
            f"reason=confirmation_required expected_sha_short={expected_sha_short}"
        )
        return 2

    settings = load_settings()
    missing = []
    if not settings.supabase_url:
        missing.append("SUPABASE_URL")
    if settings.supabase_secret_key is None:
        missing.append("SUPABASE_SECRET_KEY")
    if missing:
        print(
            "FINAL=SKIP render_worker_redeploy "
            f"reason=hosted_supabase_env_missing missing={','.join(missing)} "
            f"expected_sha_short={expected_sha_short}"
        )
        return 2
    assert settings.supabase_url is not None
    assert settings.supabase_secret_key is not None

    try:
        summary = redeploy_render_worker(
            hook_url=hook_url,
            expected_sha=normalized_sha,
            supabase_url=settings.supabase_url,
            supabase_secret_key=settings.supabase_secret_key.get_secret_value(),
            hook_client=hook_client,
            heartbeat_client=heartbeat_client,
            heartbeat_fetcher=heartbeat_fetcher,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            hook_timeout_sec=args.hook_timeout_sec,
            poll_interval_sec=args.poll_interval_sec,
            poll_timeout_sec=args.poll_timeout_sec,
            max_age_seconds=args.max_age_seconds,
        )
    except (RenderDeployHookError, RenderWorkerRedeployError) as exc:
        print(
            "FINAL=FAIL render_worker_redeploy "
            f"reason={exc} expected_sha_short={expected_sha_short}"
        )
        return 1

    if summary.deploy_status_code is not None:
        print(
            "FINAL=PASS render_deploy_hook "
            f"expected_sha_short={summary.expected_sha_short} "
            f"status_code={summary.deploy_status_code}"
        )
    print(_format_summary(summary))
    return 0 if summary.result == "PASS" else 1


def _summary_from_freshness(
    *,
    result: Literal["PASS", "FAIL"],
    reason: str | None,
    freshness: WorkerReleaseFreshnessSummary,
    deploy_status_code: int,
    attempts: int,
) -> RenderWorkerRedeploySummary:
    return RenderWorkerRedeploySummary(
        result=result,
        reason=reason,
        expected_sha_short=freshness.expected_sha_short,
        observed_sha_short=freshness.observed_sha_short,
        heartbeat_age_sec=freshness.heartbeat_age_sec,
        max_age_sec=freshness.max_age_sec,
        deploy_status_code=deploy_status_code,
        attempts=attempts,
    )


def _format_summary(summary: RenderWorkerRedeploySummary) -> str:
    parts = [f"FINAL={summary.result}", "render_worker_redeploy"]
    if summary.reason is not None:
        parts.append(f"reason={summary.reason}")
    parts.extend(
        [
            "deploy_status_code="
            + ("n/a" if summary.deploy_status_code is None else str(summary.deploy_status_code)),
            f"expected_sha_short={summary.expected_sha_short}",
            "observed_sha_short=" + (summary.observed_sha_short or "n/a"),
            "heartbeat_age_sec="
            + ("n/a" if summary.heartbeat_age_sec is None else str(summary.heartbeat_age_sec)),
            f"max_age_sec={summary.max_age_sec}",
            f"attempts={summary.attempts}",
        ]
    )
    return " ".join(parts)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Trigger a manual Render deploy hook and poll hosted worker heartbeat "
            "until the expected Git commit is observed."
        )
    )
    parser.add_argument("--hook-url", default=None, help="Render deploy hook URL")
    parser.add_argument(
        "--expected-sha",
        default=None,
        help="Expected Git commit SHA. Defaults to HEAD from --repo-root or cwd.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--hook-timeout-sec", type=float, default=10.0)
    parser.add_argument(
        "--poll-interval-sec",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--poll-timeout-sec",
        type=float,
        default=DEFAULT_POLL_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually call the deploy hook. Omit to validate configuration only.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())

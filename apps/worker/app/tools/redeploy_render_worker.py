from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx

from app.config import load_settings
from app.domain.common.json import JsonObject
from app.infrastructure.supabase_headers import supabase_api_headers
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
DEFAULT_PAUSE_TIMEOUT_SECONDS = 120.0


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
    pause_attempts: int
    deployment_locked: bool
    deployment_unlocked: bool


def begin_worker_deployment(
    *,
    supabase_url: str,
    supabase_secret_key: str,
    target_sha: str,
    client: httpx.Client | None = None,
) -> None:
    _deployment_lock_rpc(
        supabase_url=supabase_url,
        supabase_secret_key=supabase_secret_key,
        rpc_name="begin_worker_deployment",
        payload={"target_sha": target_sha},
        expected_lock=True,
        client=client,
    )


def complete_worker_deployment(
    *,
    supabase_url: str,
    supabase_secret_key: str,
    target_sha: str,
    max_age_seconds: int,
    client: httpx.Client | None = None,
) -> None:
    _deployment_lock_rpc(
        supabase_url=supabase_url,
        supabase_secret_key=supabase_secret_key,
        rpc_name="complete_worker_deployment",
        payload={"target_sha": target_sha, "max_age_seconds": max_age_seconds},
        expected_lock=False,
        client=client,
    )


def mark_worker_deployment_triggered(
    *,
    supabase_url: str,
    supabase_secret_key: str,
    target_sha: str,
    client: httpx.Client | None = None,
) -> datetime:
    result = _deployment_lock_rpc(
        supabase_url=supabase_url,
        supabase_secret_key=supabase_secret_key,
        rpc_name="mark_worker_deployment_triggered",
        payload={"target_sha": target_sha},
        expected_lock=True,
        client=client,
    )
    triggered_at = _heartbeat_created_at(result.get("deployment_triggered_at"))
    if result.get("deployment_triggered") is not True or triggered_at is None:
        raise RenderWorkerRedeployError(
            "mark_worker_deployment_triggered_response_invalid"
        )
    return triggered_at


def abort_worker_deployment(
    *,
    supabase_url: str,
    supabase_secret_key: str,
    target_sha: str,
    client: httpx.Client | None = None,
) -> None:
    _deployment_lock_rpc(
        supabase_url=supabase_url,
        supabase_secret_key=supabase_secret_key,
        rpc_name="abort_worker_deployment",
        payload={"target_sha": target_sha},
        expected_lock=False,
        client=client,
    )


def redeploy_render_worker(
    *,
    hook_url: str,
    expected_sha: str,
    supabase_url: str,
    supabase_secret_key: str,
    hook_client: httpx.Client | None = None,
    deployment_client: httpx.Client | None = None,
    heartbeat_client: httpx.Client | None = None,
    heartbeat_fetcher: HeartbeatFetcher = fetch_latest_worker_heartbeat,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    hook_timeout_sec: float = 10.0,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout_sec: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    pause_timeout_sec: float = DEFAULT_PAUSE_TIMEOUT_SECONDS,
    max_age_seconds: int = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
) -> RenderWorkerRedeploySummary:
    normalized_sha = _normalize_sha(expected_sha, "expected_sha")
    if len(normalized_sha) not in {40, 64}:
        raise RenderWorkerRedeployError("expected_sha_must_be_full")
    if not math.isfinite(hook_timeout_sec) or hook_timeout_sec <= 0:
        raise RenderWorkerRedeployError("hook_timeout_sec_must_be_positive")
    if not math.isfinite(poll_interval_sec) or poll_interval_sec <= 0:
        raise RenderWorkerRedeployError("poll_interval_sec_must_be_positive")
    if not math.isfinite(poll_timeout_sec) or poll_timeout_sec < 0:
        raise RenderWorkerRedeployError("poll_timeout_sec_must_be_non_negative")
    if not math.isfinite(pause_timeout_sec) or pause_timeout_sec < 0:
        raise RenderWorkerRedeployError("pause_timeout_sec_must_be_non_negative")
    if max_age_seconds <= 0 or max_age_seconds > 3600:
        raise RenderWorkerRedeployError("max_age_seconds_must_be_between_1_and_3600")

    expected_sha_short = _short_sha(normalized_sha)
    try:
        begin_worker_deployment(
            supabase_url=supabase_url,
            supabase_secret_key=supabase_secret_key,
            target_sha=normalized_sha,
            client=deployment_client,
        )
    except RenderWorkerRedeployError as exc:
        return RenderWorkerRedeploySummary(
            result="FAIL",
            reason=str(exc),
            expected_sha_short=expected_sha_short,
            observed_sha_short=None,
            heartbeat_age_sec=None,
            max_age_sec=max_age_seconds,
            deploy_status_code=None,
            attempts=0,
            pause_attempts=0,
            deployment_locked=False,
            deployment_unlocked=False,
        )
    paused, pause_attempts, pause_reason = _wait_for_deployment_pause(
        supabase_url=supabase_url,
        supabase_secret_key=supabase_secret_key,
        target_sha=normalized_sha,
        heartbeat_client=heartbeat_client,
        heartbeat_fetcher=heartbeat_fetcher,
        now_fn=now_fn,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
        poll_interval_sec=poll_interval_sec,
        timeout_sec=pause_timeout_sec,
        max_age_seconds=max_age_seconds,
    )
    if not paused:
        return RenderWorkerRedeploySummary(
            result="FAIL",
            reason="deployment_pause_timeout," + (pause_reason or "pause_not_observed"),
            expected_sha_short=expected_sha_short,
            observed_sha_short=None,
            heartbeat_age_sec=None,
            max_age_sec=max_age_seconds,
            deploy_status_code=None,
            attempts=0,
            pause_attempts=pause_attempts,
            deployment_locked=True,
            deployment_unlocked=False,
        )
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
            pause_attempts=pause_attempts,
            deployment_locked=True,
            deployment_unlocked=False,
        )

    try:
        deployment_triggered_at = mark_worker_deployment_triggered(
            supabase_url=supabase_url,
            supabase_secret_key=supabase_secret_key,
            target_sha=normalized_sha,
            client=deployment_client,
        )
    except RenderWorkerRedeployError as exc:
        return RenderWorkerRedeploySummary(
            result="FAIL",
            reason=str(exc),
            expected_sha_short=expected_sha_short,
            observed_sha_short=None,
            heartbeat_age_sec=None,
            max_age_sec=max_age_seconds,
            deploy_status_code=deploy_result.status_code,
            attempts=0,
            pause_attempts=pause_attempts,
            deployment_locked=True,
            deployment_unlocked=False,
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
            heartbeat_created_at = (
                _heartbeat_created_at(row.get("created_at")) if row is not None else None
            )
            if (
                heartbeat_created_at is None
                or heartbeat_created_at <= deployment_triggered_at
            ):
                last_summary = None
                last_failure_reason = "heartbeat_precedes_deploy_trigger"
                raise WorkerReleaseFreshnessError(last_failure_reason)
            last_summary = verify_worker_release_freshness(
                row,
                expected_sha=normalized_sha,
                now=now_fn(),
                max_age_seconds=max_age_seconds,
            )
            last_failure_reason = last_summary.reason
            if last_summary.result == "PASS":
                try:
                    complete_worker_deployment(
                        supabase_url=supabase_url,
                        supabase_secret_key=supabase_secret_key,
                        target_sha=normalized_sha,
                        max_age_seconds=max_age_seconds,
                        client=deployment_client,
                    )
                except RenderWorkerRedeployError:
                    return _summary_from_freshness(
                        result="FAIL",
                        reason="deployment_unlock_failed",
                        freshness=last_summary,
                        deploy_status_code=deploy_result.status_code,
                        attempts=attempts,
                        pause_attempts=pause_attempts,
                        deployment_unlocked=False,
                    )
                return _summary_from_freshness(
                    result="PASS",
                    reason=None,
                    freshness=last_summary,
                    deploy_status_code=deploy_result.status_code,
                    attempts=attempts,
                    pause_attempts=pause_attempts,
                    deployment_unlocked=True,
                )
        except WorkerReleaseFreshnessError:
            last_summary = None
            if last_failure_reason != "heartbeat_precedes_deploy_trigger":
                last_failure_reason = "freshness_verification_unavailable"
        except (httpx.HTTPError, ValueError):
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
            pause_attempts=pause_attempts,
            deployment_unlocked=False,
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
        pause_attempts=pause_attempts,
        deployment_locked=True,
        deployment_unlocked=False,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    hook_client: httpx.Client | None = None,
    deployment_client: httpx.Client | None = None,
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
    hook_url = env.get(RENDER_DEPLOY_HOOK_ENV)
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
            deployment_client=deployment_client,
            heartbeat_client=heartbeat_client,
            heartbeat_fetcher=heartbeat_fetcher,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            hook_timeout_sec=args.hook_timeout_sec,
            poll_interval_sec=args.poll_interval_sec,
            poll_timeout_sec=args.poll_timeout_sec,
            pause_timeout_sec=args.pause_timeout_sec,
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
    pause_attempts: int,
    deployment_unlocked: bool,
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
        pause_attempts=pause_attempts,
        deployment_locked=True,
        deployment_unlocked=deployment_unlocked,
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
            f"pause_attempts={summary.pause_attempts}",
            f"deployment_locked={int(summary.deployment_locked)}",
            f"deployment_unlocked={int(summary.deployment_unlocked)}",
        ]
    )
    return " ".join(parts)


def _wait_for_deployment_pause(
    *,
    supabase_url: str,
    supabase_secret_key: str,
    target_sha: str,
    heartbeat_client: httpx.Client | None,
    heartbeat_fetcher: HeartbeatFetcher,
    now_fn: Callable[[], datetime],
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
    poll_interval_sec: float,
    timeout_sec: float,
    max_age_seconds: int,
) -> tuple[bool, int, str | None]:
    deadline = monotonic_fn() + timeout_sec
    attempts = 0
    last_reason: str | None = None
    while True:
        attempts += 1
        try:
            row = heartbeat_fetcher(
                supabase_url=supabase_url,
                supabase_secret_key=supabase_secret_key,
                client=heartbeat_client,
            )
            last_reason = _deployment_pause_reason(
                row,
                target_sha=target_sha,
                now=now_fn(),
                max_age_seconds=max_age_seconds,
            )
            if last_reason is None:
                return True, attempts, None
        except (httpx.HTTPError, ValueError):
            last_reason = "pause_verification_unavailable"
        now_monotonic = monotonic_fn()
        if timeout_sec == 0 or now_monotonic >= deadline:
            return False, attempts, last_reason
        sleep_for = min(poll_interval_sec, max(0.0, deadline - now_monotonic))
        sleep_fn(sleep_for)


def _deployment_pause_reason(
    row: JsonObject | None,
    *,
    target_sha: str,
    now: datetime,
    max_age_seconds: int,
) -> str | None:
    if row is None:
        return "heartbeat_missing"
    if row.get("status") != "ok":
        return "heartbeat_not_ok"
    details = row.get("details")
    if not isinstance(details, dict):
        return "heartbeat_details_missing"
    if details.get("deployment_lock") is not True:
        return "deployment_lock_not_observed"
    if details.get("deployment_target_sha") != target_sha:
        return "deployment_target_not_observed"
    created_at = _heartbeat_created_at(row.get("created_at"))
    if created_at is None:
        return "heartbeat_timestamp_invalid"
    age_seconds = (now.astimezone(UTC) - created_at).total_seconds()
    if age_seconds < 0:
        return "heartbeat_timestamp_future"
    if age_seconds > max_age_seconds:
        return "heartbeat_stale"
    return None


def _heartbeat_created_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _deployment_lock_rpc(
    *,
    supabase_url: str,
    supabase_secret_key: str,
    rpc_name: str,
    payload: dict[str, object],
    expected_lock: bool,
    client: httpx.Client | None,
) -> JsonObject:
    base_url = _hosted_supabase_url(supabase_url)
    close_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        response = http_client.post(
            f"{base_url}/rest/v1/rpc/{rpc_name}",
            headers=supabase_api_headers(supabase_secret_key)
            | {"content-type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise RenderWorkerRedeployError(f"{rpc_name}_unavailable") from exc
    finally:
        if close_client:
            http_client.close()
    if response.status_code != 200:
        raise RenderWorkerRedeployError(f"{rpc_name}_failed")
    try:
        result = response.json()
    except ValueError as exc:
        raise RenderWorkerRedeployError(f"{rpc_name}_response_invalid") from exc
    target_sha = payload.get("target_sha")
    expected_short = target_sha[:12] if isinstance(target_sha, str) else None
    if (
        not isinstance(result, dict)
        or result.get("deployment_lock") is not expected_lock
        or result.get("target_sha_short") != expected_short
    ):
        raise RenderWorkerRedeployError(f"{rpc_name}_response_invalid")
    return result


def _hosted_supabase_url(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RenderWorkerRedeployError("supabase_url_invalid") from exc
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".supabase.co")
        or hostname.count(".") != 2
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or re.fullmatch(r"[a-z0-9-]{3,63}\.supabase\.co", hostname) is None
    ):
        raise RenderWorkerRedeployError("supabase_url_invalid")
    return parsed.geturl().rstrip("/")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Trigger a manual Render deploy hook and poll hosted worker heartbeat "
            "until the expected Git commit is observed."
        )
    )
    raw_args = list(argv) if argv is not None else list(sys.argv[1:])
    for token in raw_args:
        if token.partition("=")[0] == "--hook-url":
            parser.error(f"--hook-url is forbidden; use {RENDER_DEPLOY_HOOK_ENV}")
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
        "--pause-timeout-sec",
        type=float,
        default=DEFAULT_PAUSE_TIMEOUT_SECONDS,
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

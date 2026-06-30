from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import load_settings
from app.domain.common.json import JsonObject, JsonValue, json_object

GIT_SHA_RE = r"[A-Fa-f0-9]{12,64}"
DEFAULT_MAX_HEARTBEAT_AGE_SECONDS = 300
LATEST_HEARTBEAT_QUERY = "select=created_at,status,details&order=created_at.desc&limit=1"


class WorkerReleaseFreshnessError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerReleaseFreshnessSummary:
    result: str
    reason: str | None
    expected_sha_short: str
    observed_sha_short: str | None
    heartbeat_age_sec: int | None
    max_age_sec: int


def verify_worker_release_freshness(
    row: Mapping[str, JsonValue] | None,
    *,
    expected_sha: str,
    now: datetime,
    max_age_seconds: int = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
) -> WorkerReleaseFreshnessSummary:
    normalized_expected = _normalize_sha(expected_sha, "expected_sha")
    if max_age_seconds <= 0:
        raise WorkerReleaseFreshnessError("max_age_seconds_must_be_positive")
    if row is None:
        return WorkerReleaseFreshnessSummary(
            result="FAIL",
            reason="heartbeat_missing",
            expected_sha_short=_short_sha(normalized_expected),
            observed_sha_short=None,
            heartbeat_age_sec=None,
            max_age_sec=max_age_seconds,
        )

    observed_sha = _heartbeat_release_sha(row)
    created_at = _datetime_value(row.get("created_at"))
    heartbeat_age_sec = _heartbeat_age_seconds(created_at, now)
    reasons: list[str] = []
    observed_sha_short: str | None = None
    if observed_sha is None:
        reasons.append("release_sha_missing")
    else:
        observed_sha_short = _short_sha(observed_sha)
        if observed_sha != normalized_expected:
            reasons.append("release_sha_mismatch")
    if heartbeat_age_sec is None:
        reasons.append("heartbeat_timestamp_invalid")
    elif heartbeat_age_sec > max_age_seconds:
        reasons.append("heartbeat_stale")

    if reasons:
        return WorkerReleaseFreshnessSummary(
            result="FAIL",
            reason=",".join(reasons),
            expected_sha_short=_short_sha(normalized_expected),
            observed_sha_short=observed_sha_short,
            heartbeat_age_sec=heartbeat_age_sec,
            max_age_sec=max_age_seconds,
        )
    return WorkerReleaseFreshnessSummary(
        result="PASS",
        reason=None,
        expected_sha_short=_short_sha(normalized_expected),
        observed_sha_short=observed_sha_short,
        heartbeat_age_sec=heartbeat_age_sec,
        max_age_sec=max_age_seconds,
    )


def fetch_latest_worker_heartbeat(
    *,
    supabase_url: str,
    supabase_secret_key: str,
    client: httpx.Client | None = None,
) -> JsonObject | None:
    base_url = supabase_url.rstrip("/") + "/rest/v1"
    headers = {
        "apikey": supabase_secret_key,
        "authorization": "Bearer " + supabase_secret_key,
        "accept": "application/json",
    }
    close_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        response = http_client.get(
            f"{base_url}/worker_heartbeats?{LATEST_HEARTBEAT_QUERY}",
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if close_client:
            http_client.close()
    if not isinstance(payload, list) or not payload:
        return None
    return json_object(payload[0])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the hosted worker heartbeat is running the expected release."
    )
    parser.add_argument(
        "--expected-sha",
        default=None,
        help="Expected Git commit SHA. Defaults to HEAD from --repo-root or cwd.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    )
    args = parser.parse_args(argv)

    try:
        expected_sha = args.expected_sha or _git_head(args.repo_root)
        _normalize_sha(expected_sha, "expected_sha")
    except WorkerReleaseFreshnessError as exc:
        print(f"FINAL=FAIL worker_release_freshness reason={exc}")
        return 1

    settings = load_settings()
    supabase_url = settings.supabase_url
    supabase_secret_key = settings.supabase_secret_key
    missing = []
    if not supabase_url:
        missing.append("SUPABASE_URL")
    if supabase_secret_key is None:
        missing.append("SUPABASE_SECRET_KEY")
    if missing:
        print(
            "FINAL=SKIP worker_release_freshness "
            f"reason=hosted_supabase_env_missing missing={','.join(missing)}"
        )
        return 1
    assert supabase_url is not None
    assert supabase_secret_key is not None

    try:
        row = fetch_latest_worker_heartbeat(
            supabase_url=supabase_url,
            supabase_secret_key=supabase_secret_key.get_secret_value(),
        )
        summary = verify_worker_release_freshness(
            row,
            expected_sha=expected_sha,
            now=datetime.now(UTC),
            max_age_seconds=args.max_age_seconds,
        )
    except (WorkerReleaseFreshnessError, httpx.HTTPError, ValueError):
        print("FINAL=FAIL worker_release_freshness reason=verification_unavailable")
        return 1

    print(_format_summary(summary))
    return 0 if summary.result == "PASS" else 1


def _format_summary(summary: WorkerReleaseFreshnessSummary) -> str:
    parts = [
        f"FINAL={summary.result}",
        "worker_release_freshness",
    ]
    if summary.reason is not None:
        parts.append(f"reason={summary.reason}")
    parts.extend(
        [
            f"expected_sha_short={summary.expected_sha_short}",
            "observed_sha_short=" + (summary.observed_sha_short or "n/a"),
            "heartbeat_age_sec="
            + ("n/a" if summary.heartbeat_age_sec is None else str(summary.heartbeat_age_sec)),
            f"max_age_sec={summary.max_age_sec}",
        ]
    )
    return " ".join(parts)


def _heartbeat_release_sha(row: Mapping[str, JsonValue]) -> str | None:
    details = row.get("details")
    if not isinstance(details, Mapping):
        return None
    value = details.get("release_sha")
    if not isinstance(value, str):
        return None
    try:
        return _normalize_sha(value, "release_sha")
    except WorkerReleaseFreshnessError:
        return None


def _datetime_value(value: JsonValue | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _heartbeat_age_seconds(created_at: datetime | None, now: datetime) -> int | None:
    if created_at is None:
        return None
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    age = int((now_utc.astimezone(UTC) - created_at).total_seconds())
    if age < 0:
        return None
    return age


def _normalize_sha(value: str, field: str) -> str:
    if not re.fullmatch(GIT_SHA_RE, value):
        raise WorkerReleaseFreshnessError(f"{field}_invalid")
    return value.lower()


def _short_sha(value: str) -> str:
    return value[:12]


def _git_head(repo_root: Path) -> str:
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorkerReleaseFreshnessError("git_head_unavailable") from exc
    try:
        return output.decode("utf-8").strip()
    except UnicodeError as exc:
        raise WorkerReleaseFreshnessError("git_head_unavailable") from exc


if __name__ == "__main__":
    raise SystemExit(main())

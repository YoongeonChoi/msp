from __future__ import annotations

import argparse
import ipaddress
import math
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

EXPECTED_DENIED_STATUSES = {400, 401, 403}


@dataclass(frozen=True, slots=True)
class HostedLiveEnableConfig:
    supabase_url: str
    publishable_key: str
    secret_key: str
    requester_jwt: str
    reviewer_jwt: str
    timeout_sec: float


@dataclass(frozen=True, slots=True)
class HostedLiveEnableResult:
    requester_admin_ok: bool
    reviewer_admin_ok: bool
    request_created: bool
    self_review_denied: bool
    review_accepted: bool
    activation_consumed_once: bool
    second_activation_denied: bool
    command_id: str


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = _parse_args(argv)
    env = environ or os.environ
    config, missing = _config_from_env(args, env)
    if missing:
        print("FINAL=SKIP hosted_live_enable_env_missing missing=" + ",".join(missing))
        return 2

    assert config is not None
    try:
        with httpx.Client(timeout=config.timeout_sec) as client:
            result = run_checks(config, client=client)
    except Exception as exc:
        print("FINAL=FAIL hosted_live_enable_flow")
        print(_safe_error(exc, _config_secret_values(config)))
        return 1

    print(format_result(result))
    return 0


def run_checks(
    config: HostedLiveEnableConfig,
    *,
    client: httpx.Client,
) -> HostedLiveEnableResult:
    _validate_config(config)
    supabase_url = _normalize_url(config.supabase_url)
    requester_id = _get_user_id(
        client,
        supabase_url,
        config.publishable_key,
        config.requester_jwt,
        label="requester",
    )
    reviewer_id = _get_user_id(
        client,
        supabase_url,
        config.publishable_key,
        config.reviewer_jwt,
        label="reviewer",
    )
    if requester_id == reviewer_id:
        raise RuntimeError("requester_and_reviewer_must_be_different_users")

    _expect_admin_role(
        client, supabase_url, config.publishable_key, config.requester_jwt, requester_id
    )
    _expect_admin_role(
        client, supabase_url, config.publishable_key, config.reviewer_jwt, reviewer_id
    )

    command_id = ""
    try:
        _force_live_disabled(client, supabase_url, config.secret_key)
        _assert_no_unapplied_live_enable(client, supabase_url, config.secret_key)
        command_id = _create_live_enable_request(
            client,
            supabase_url,
            config.publishable_key,
            config.requester_jwt,
        )
        _expect_self_review_denied(
            client,
            supabase_url,
            config.publishable_key,
            config.requester_jwt,
            command_id,
        )
        _accept_live_enable_request(
            client,
            supabase_url,
            config.publishable_key,
            config.reviewer_jwt,
            command_id,
            requester_id=requester_id,
            reviewer_id=reviewer_id,
        )
        _activate_live_once(
            client,
            supabase_url,
            config.publishable_key,
            config.reviewer_jwt,
        )
        _expect_command_applied_once(
            client,
            supabase_url,
            config.secret_key,
            command_id,
            requester_id=requester_id,
            reviewer_id=reviewer_id,
        )
        _force_live_disabled(client, supabase_url, config.secret_key)
        _expect_second_activation_denied(
            client,
            supabase_url,
            config.publishable_key,
            config.reviewer_jwt,
        )
    finally:
        _force_live_disabled(client, supabase_url, config.secret_key, raise_on_error=False)

    return HostedLiveEnableResult(
        requester_admin_ok=True,
        reviewer_admin_ok=True,
        request_created=True,
        self_review_denied=True,
        review_accepted=True,
        activation_consumed_once=True,
        second_activation_denied=True,
        command_id=command_id,
    )


def format_result(result: HostedLiveEnableResult) -> str:
    return (
        "FINAL=PASS hosted_live_enable_flow "
        f"requester_admin={int(result.requester_admin_ok)} "
        f"reviewer_admin={int(result.reviewer_admin_ok)} "
        f"request_created={int(result.request_created)} "
        f"self_review_denied={int(result.self_review_denied)} "
        f"review_accepted={int(result.review_accepted)} "
        f"activation_consumed_once={int(result.activation_consumed_once)} "
        f"second_activation_denied={int(result.second_activation_denied)}"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify hosted Supabase live-enable request/review/apply flow with two "
            "real admin user JWTs without printing secrets."
        ),
    )
    parser.add_argument("--url", default=None, help="Supabase project URL")
    parser.add_argument("--publishable-key", default=None, help="Supabase publishable/anon key")
    parser.add_argument(
        "--secret-key", default=None, help="Supabase worker secret/service role key"
    )
    parser.add_argument("--requester-jwt", default=None, help="Requester admin user access token")
    parser.add_argument("--reviewer-jwt", default=None, help="Reviewer admin user access token")
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    return parser.parse_args(argv)


def _config_from_env(
    args: argparse.Namespace,
    env: Mapping[str, str],
) -> tuple[HostedLiveEnableConfig | None, list[str]]:
    url = args.url or env.get("SUPABASE_URL") or env.get("VITE_SUPABASE_URL")
    publishable_key = (
        args.publishable_key
        or env.get("SUPABASE_PUBLISHABLE_KEY")
        or env.get("VITE_SUPABASE_PUBLISHABLE_KEY")
        or env.get("SUPABASE_ANON_KEY")
    )
    secret_key = args.secret_key or env.get("SUPABASE_SECRET_KEY")
    requester_jwt = args.requester_jwt or env.get("SUPABASE_LIVE_REQUESTER_JWT")
    reviewer_jwt = args.reviewer_jwt or env.get("SUPABASE_LIVE_REVIEWER_JWT")
    values = {
        "SUPABASE_URL": url,
        "SUPABASE_PUBLISHABLE_KEY": publishable_key,
        "SUPABASE_SECRET_KEY": secret_key,
        "SUPABASE_LIVE_REQUESTER_JWT": requester_jwt,
        "SUPABASE_LIVE_REVIEWER_JWT": reviewer_jwt,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        return None, missing
    return (
        HostedLiveEnableConfig(
            supabase_url=str(url),
            publishable_key=str(publishable_key),
            secret_key=str(secret_key),
            requester_jwt=str(requester_jwt),
            reviewer_jwt=str(reviewer_jwt),
            timeout_sec=float(args.timeout_sec),
        ),
        [],
    )


def _validate_config(config: HostedLiveEnableConfig) -> None:
    if not math.isfinite(config.timeout_sec) or config.timeout_sec <= 0:
        raise RuntimeError("timeout_sec_must_be_positive")
    if config.publishable_key == config.secret_key:
        raise RuntimeError("supabase_publishable_and_secret_keys_must_be_distinct")
    if config.requester_jwt == config.reviewer_jwt:
        raise RuntimeError("requester_and_reviewer_jwts_must_be_distinct")
    if config.requester_jwt in {config.publishable_key, config.secret_key}:
        raise RuntimeError("requester_jwt_must_not_reuse_supabase_key")
    if config.reviewer_jwt in {config.publishable_key, config.secret_key}:
        raise RuntimeError("reviewer_jwt_must_not_reuse_supabase_key")


def _get_user_id(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    jwt: str,
    *,
    label: str,
) -> str:
    response = client.get(
        f"{supabase_url}/auth/v1/user",
        headers=_headers(publishable_key, jwt),
    )
    _expect_status(response, 200, f"{label}_auth_user_failed")
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
        raise RuntimeError(f"{label}_auth_user_missing_id")
    return data["id"]


def _expect_admin_role(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    jwt: str,
    user_id: str,
) -> None:
    response = client.get(
        f"{supabase_url}/rest/v1/user_roles",
        headers=_headers(publishable_key, jwt),
        params={"select": "role", "user_id": f"eq.{user_id}"},
    )
    _expect_status(response, 200, "admin_role_read_failed")
    rows = response.json()
    if not isinstance(rows, list) or not any(
        isinstance(row, dict) and row.get("role") == "admin" for row in rows
    ):
        raise RuntimeError("admin_role_missing")


def _force_live_disabled(
    client: httpx.Client,
    supabase_url: str,
    secret_key: str,
    *,
    raise_on_error: bool = True,
) -> None:
    response = client.patch(
        f"{supabase_url}/rest/v1/bot_settings",
        headers=_headers(secret_key, secret_key),
        params={"id": "eq.singleton"},
        json={"enabled": False, "mode": "paper", "live_order_allowed": False},
    )
    if raise_on_error:
        _expect_allowed_status(response, {200, 204}, "disable_live_failed")


def _assert_no_unapplied_live_enable(
    client: httpx.Client,
    supabase_url: str,
    secret_key: str,
) -> None:
    response = client.get(
        f"{supabase_url}/rest/v1/manual_commands",
        headers=_headers(secret_key, secret_key),
        params={
            "select": "id",
            "command_type": "eq.request_live_enable",
            "status": "eq.accepted",
            "applied_at": "is.null",
            "expires_at": f"gt.{_utc_now_iso()}",
            "limit": "1",
        },
    )
    _expect_status(response, 200, "preexisting_live_enable_check_failed")
    rows = response.json()
    if isinstance(rows, list) and rows:
        raise RuntimeError("preexisting_unapplied_live_enable_command")


def _create_live_enable_request(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    requester_jwt: str,
) -> str:
    response = client.post(
        f"{supabase_url}/rest/v1/manual_commands",
        headers=_headers(publishable_key, requester_jwt, prefer="return=representation"),
        params={"select": "id,status,requested_by,expires_at,payload"},
        json={
            "command_type": "request_live_enable",
            "status": "pending",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            "payload": {
                "provider_contract_version": "hosted-live-enable-flow-verifier",
                "risk_report_id": "hosted-live-enable-flow-verifier",
                "release_version": "hosted-live-enable-flow-verifier",
            },
        },
    )
    _expect_status(response, 201, "live_enable_request_insert_failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("live_enable_request_insert_unexpected_response")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or not isinstance(row.get("id"), str)
        or row.get("status") != "pending"
    ):
        raise RuntimeError("live_enable_request_insert_invalid_row")
    return row["id"]


def _expect_self_review_denied(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    requester_jwt: str,
    command_id: str,
) -> None:
    response = client.patch(
        f"{supabase_url}/rest/v1/manual_commands",
        headers=_headers(publishable_key, requester_jwt, prefer="return=representation"),
        params={"id": f"eq.{command_id}", "select": "id,status"},
        json={"status": "accepted"},
    )
    if response.status_code not in EXPECTED_DENIED_STATUSES:
        raise RuntimeError(f"live_enable_self_review_not_denied status={response.status_code}")


def _accept_live_enable_request(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    reviewer_jwt: str,
    command_id: str,
    *,
    requester_id: str,
    reviewer_id: str,
) -> None:
    response = client.patch(
        f"{supabase_url}/rest/v1/manual_commands",
        headers=_headers(publishable_key, reviewer_jwt, prefer="return=representation"),
        params={
            "id": f"eq.{command_id}",
            "command_type": "eq.request_live_enable",
            "status": "eq.pending",
            "select": "id,status,requested_by,reviewed_by,reviewed_at,applied_at",
        },
        json={"status": "accepted"},
    )
    _expect_status(response, 200, "live_enable_review_accept_failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("live_enable_review_accept_unexpected_response")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or row.get("id") != command_id
        or row.get("status") != "accepted"
        or row.get("requested_by") != requester_id
        or row.get("reviewed_by") != reviewer_id
        or not row.get("reviewed_at")
        or row.get("applied_at") is not None
    ):
        raise RuntimeError("live_enable_review_accept_invalid_row")


def _activate_live_once(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    reviewer_jwt: str,
) -> None:
    response = client.patch(
        f"{supabase_url}/rest/v1/bot_settings",
        headers=_headers(publishable_key, reviewer_jwt, prefer="return=representation"),
        params={"id": "eq.singleton", "select": "id,enabled,mode,live_order_allowed"},
        json={"enabled": True, "mode": "live", "live_order_allowed": True},
    )
    _expect_status(response, 200, "live_enable_activation_failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("live_enable_activation_unexpected_response")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or row.get("id") != "singleton"
        or row.get("enabled") is not True
        or row.get("mode") != "live"
        or row.get("live_order_allowed") is not True
    ):
        raise RuntimeError("live_enable_activation_invalid_row")


def _expect_command_applied_once(
    client: httpx.Client,
    supabase_url: str,
    secret_key: str,
    command_id: str,
    *,
    requester_id: str,
    reviewer_id: str,
) -> None:
    response = client.get(
        f"{supabase_url}/rest/v1/manual_commands",
        headers=_headers(secret_key, secret_key),
        params={
            "select": "id,status,requested_by,reviewed_by,reviewed_at,applied_at",
            "id": f"eq.{command_id}",
        },
    )
    _expect_status(response, 200, "live_enable_applied_check_failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("live_enable_applied_check_unexpected_response")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or row.get("id") != command_id
        or row.get("status") != "applied"
        or row.get("requested_by") != requester_id
        or row.get("reviewed_by") != reviewer_id
        or not row.get("reviewed_at")
        or not row.get("applied_at")
    ):
        raise RuntimeError("live_enable_applied_check_invalid_row")


def _expect_second_activation_denied(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    reviewer_jwt: str,
) -> None:
    response = client.patch(
        f"{supabase_url}/rest/v1/bot_settings",
        headers=_headers(publishable_key, reviewer_jwt, prefer="return=representation"),
        params={"id": "eq.singleton", "select": "id,enabled,mode,live_order_allowed"},
        json={"enabled": True, "mode": "live", "live_order_allowed": True},
    )
    if response.status_code not in EXPECTED_DENIED_STATUSES:
        raise RuntimeError(f"second_live_enable_not_denied status={response.status_code}")


def _headers(key: str, bearer: str, *, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    if prefer is not None:
        headers["Prefer"] = prefer
    return headers


def _expect_status(response: httpx.Response, status_code: int, label: str) -> None:
    if response.status_code != status_code:
        raise RuntimeError(f"{label} status={response.status_code}")


def _expect_allowed_status(response: httpx.Response, statuses: set[int], label: str) -> None:
    if response.status_code not in statuses:
        raise RuntimeError(f"{label} status={response.status_code}")


def _normalize_url(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme != "https":
        raise RuntimeError("supabase_url_must_be_https")
    if not parsed.hostname:
        raise RuntimeError("supabase_url_missing_host")
    if parsed.username or parsed.password:
        raise RuntimeError("supabase_url_must_not_include_credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError("supabase_url_must_not_include_query_or_fragment")
    if parsed.path not in {"", "/"}:
        raise RuntimeError("supabase_url_must_not_include_path")
    _reject_non_hosted_hostname(parsed.hostname)
    return parsed.geturl().rstrip("/")


def _reject_non_hosted_hostname(hostname: str) -> None:
    normalized = hostname.strip().lower().rstrip(".")
    if (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or normalized.endswith(".local")
        or normalized.endswith(".test")
        or normalized.endswith(".invalid")
        or normalized.endswith(".example")
        or "." not in normalized
    ):
        raise RuntimeError("supabase_url_must_be_hosted")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        raise RuntimeError("supabase_url_must_be_hosted")
    if not re.fullmatch(r"[a-z0-9-]+\.supabase\.co", normalized):
        raise RuntimeError("supabase_url_must_be_hosted_supabase_project")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_error(exc: Exception, secrets_to_redact: Iterable[str] = ()) -> str:
    message = str(exc)
    for secret in secrets_to_redact:
        if secret:
            message = message.replace(secret, "<redacted>")
    message = re.sub(r"(?i)(apikey=)[^&\s]+", r"\1<redacted>", message)
    message = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1<redacted>", message)
    message = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", message)
    message = re.sub(r"sb_(?:publishable|secret)_[A-Za-z0-9_]+", "sb_<redacted>", message)
    message = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "<jwt_redacted>",
        message,
    )
    if len(message) > 240:
        message = message[:237] + "..."
    return message


def _config_secret_values(config: HostedLiveEnableConfig) -> tuple[str, str, str, str]:
    return (
        config.publishable_key,
        config.secret_key,
        config.requester_jwt,
        config.reviewer_jwt,
    )


if __name__ == "__main__":
    sys.exit(main())

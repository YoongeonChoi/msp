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
from pathlib import Path
from urllib.parse import urlparse

import httpx
from _hosted_env import HostedEnvFileError, merge_env_files

EXPECTED_DENIED_STATUSES = {400, 401, 403}
ACTIVE_WORKER_HEARTBEAT_MAX_AGE_SECONDS = 120
MAX_FUTURE_HEARTBEAT_SKEW_SECONDS = 5
WORKER_ISOLATION_LOOKBACK_SECONDS = 3720
MAX_WORKER_HEARTBEAT_ROWS = 1000


@dataclass(frozen=True, slots=True)
class HostedLiveEnableConfig:
    supabase_url: str
    publishable_key: str
    secret_key: str
    requester_jwt: str
    reviewer_jwt: str
    timeout_sec: float
    staging_project_ref: str
    production_project_ref: str
    verification_target: str
    confirmation_project_ref: str


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
    try:
        env = merge_env_files(
            args.env_file,
            environ if environ is not None else os.environ,
        )
    except HostedEnvFileError as exc:
        print("FINAL=FAIL hosted_live_enable_flow")
        print(str(exc))
        return 1
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
    _assert_fresh_mock_worker_isolation(
        client,
        supabase_url,
        config.secret_key,
    )
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
        cleanup_errors: list[Exception] = []
        try:
            _force_live_disabled(client, supabase_url, config.secret_key)
        except Exception as exc:
            cleanup_errors.append(exc)
        if command_id:
            try:
                _delete_incomplete_verifier_command(
                    client,
                    supabase_url,
                    config.secret_key,
                    command_id,
                )
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]

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
        "--confirm-staging-project",
        default=None,
        help="Type the exact staging project ref to authorize this destructive drill.",
    )
    _reject_secret_cli_options(
        argv,
        parser,
        {"--secret-key", "--requester-jwt", "--reviewer-jwt"},
    )
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=[],
        help=(
            "Optional local .env file to merge before process env. "
            "Process env and non-secret explicit CLI args take precedence."
        ),
    )
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
    secret_key = env.get("SUPABASE_SECRET_KEY")
    requester_jwt = env.get("SUPABASE_LIVE_REQUESTER_JWT")
    reviewer_jwt = env.get("SUPABASE_LIVE_REVIEWER_JWT")
    staging_project_ref = env.get("SUPABASE_STAGING_PROJECT_REF")
    production_project_ref = env.get("SUPABASE_PRODUCTION_PROJECT_REF")
    verification_target = env.get("SUPABASE_LIVE_ENABLE_VERIFICATION_TARGET")
    values = {
        "SUPABASE_URL": url,
        "SUPABASE_PUBLISHABLE_KEY": publishable_key,
        "SUPABASE_SECRET_KEY": secret_key,
        "SUPABASE_LIVE_REQUESTER_JWT": requester_jwt,
        "SUPABASE_LIVE_REVIEWER_JWT": reviewer_jwt,
        "SUPABASE_STAGING_PROJECT_REF": staging_project_ref,
        "SUPABASE_PRODUCTION_PROJECT_REF": production_project_ref,
        "SUPABASE_LIVE_ENABLE_VERIFICATION_TARGET": verification_target,
        "--confirm-staging-project": args.confirm_staging_project,
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
            staging_project_ref=str(staging_project_ref),
            production_project_ref=str(production_project_ref),
            verification_target=str(verification_target),
            confirmation_project_ref=str(args.confirm_staging_project),
        ),
        [],
    )


def _reject_secret_cli_options(
    argv: list[str] | None,
    parser: argparse.ArgumentParser,
    forbidden_options: set[str],
) -> None:
    raw_args = list(argv) if argv is not None else list(sys.argv[1:])
    env_names = {
        "--secret-key": "SUPABASE_SECRET_KEY",
        "--requester-jwt": "SUPABASE_LIVE_REQUESTER_JWT",
        "--reviewer-jwt": "SUPABASE_LIVE_REVIEWER_JWT",
    }
    for token in raw_args:
        option = token.partition("=")[0]
        if option in forbidden_options:
            parser.error(f"{option} is forbidden; use {env_names[option]} or --env-file")


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
    if config.verification_target != "staging":
        raise RuntimeError("hosted_live_enable_verification_target_must_be_staging")
    if not re.fullmatch(r"[a-z0-9-]{3,63}", config.staging_project_ref):
        raise RuntimeError("staging_project_ref_invalid")
    if not re.fullmatch(r"[a-z0-9-]{3,63}", config.production_project_ref):
        raise RuntimeError("production_project_ref_invalid")
    if config.staging_project_ref == config.production_project_ref:
        raise RuntimeError("staging_and_production_project_refs_must_differ")
    project_ref = _project_ref(config.supabase_url)
    if project_ref == config.production_project_ref:
        raise RuntimeError("production_project_mutation_forbidden")
    if project_ref != config.staging_project_ref:
        raise RuntimeError("supabase_url_must_match_staging_project_ref")
    if config.confirmation_project_ref != config.staging_project_ref:
        raise RuntimeError("staging_project_confirmation_mismatch")


def _assert_fresh_mock_worker_isolation(
    client: httpx.Client,
    supabase_url: str,
    secret_key: str,
) -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=WORKER_ISOLATION_LOOKBACK_SECONDS)
    response = client.get(
        f"{supabase_url}/rest/v1/worker_heartbeats",
        headers=_headers(secret_key, secret_key),
        params={
            "select": "status,created_at,details",
            "created_at": f"gt.{cutoff.isoformat()}",
            "order": "created_at.desc",
            "limit": str(MAX_WORKER_HEARTBEAT_ROWS),
        },
    )
    _expect_status(response, 200, "worker_preflight_failed")
    rows = response.json()
    if (
        not isinstance(rows, list)
        or not rows
        or len(rows) >= MAX_WORKER_HEARTBEAT_ROWS
    ):
        raise RuntimeError("worker_preflight_response_invalid")

    parsed_rows: list[tuple[datetime, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("worker_preflight_response_invalid")
        created_at = row.get("created_at")
        details = row.get("details")
        status = row.get("status")
        if (
            not isinstance(created_at, str)
            or not isinstance(details, dict)
            or not isinstance(status, str)
        ):
            raise RuntimeError("worker_preflight_response_invalid")
        try:
            parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError("worker_preflight_timestamp_invalid") from exc
        if parsed_created_at.tzinfo is None:
            raise RuntimeError("worker_preflight_timestamp_invalid")
        normalized_created_at = parsed_created_at.astimezone(UTC)
        age_seconds = (now - normalized_created_at).total_seconds()
        if age_seconds < -MAX_FUTURE_HEARTBEAT_SKEW_SECONDS:
            raise RuntimeError("worker_preflight_timestamp_future")
        if age_seconds > WORKER_ISOLATION_LOOKBACK_SECONDS:
            raise RuntimeError("worker_preflight_response_invalid")
        if details.get("mock_providers") is not True:
            raise RuntimeError("hosted_worker_must_be_fresh_mock_only")
        parsed_rows.append((normalized_created_at, status))

    latest_created_at, latest_status = max(parsed_rows, key=lambda item: item[0])
    latest_age_seconds = (now - latest_created_at).total_seconds()
    if (
        latest_status != "ok"
        or latest_age_seconds > ACTIVE_WORKER_HEARTBEAT_MAX_AGE_SECONDS
    ):
        raise RuntimeError("hosted_worker_must_be_fresh_mock_only")


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
    user_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise RuntimeError(f"{label}_auth_user_missing_id")
    return user_id


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
) -> None:
    response = client.patch(
        f"{supabase_url}/rest/v1/bot_settings",
        headers=_headers(secret_key, secret_key, prefer="return=representation"),
        params={
            "id": "eq.singleton",
            "select": "id,enabled,mode,live_order_allowed",
        },
        json={"enabled": False, "mode": "paper", "live_order_allowed": False},
    )
    _expect_status(response, 200, "disable_live_failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("disable_live_unexpected_response")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or row.get("id") != "singleton"
        or row.get("enabled") is not False
        or row.get("mode") != "paper"
        or row.get("live_order_allowed") is not False
    ):
        raise RuntimeError("disable_live_invalid_row")


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
    if not isinstance(rows, list):
        raise RuntimeError("preexisting_live_enable_check_invalid_response")
    if rows:
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
    command_id = row["id"]
    if not isinstance(command_id, str):
        raise RuntimeError("live_enable_request_insert_invalid_row")
    return command_id


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


def _delete_incomplete_verifier_command(
    client: httpx.Client,
    supabase_url: str,
    secret_key: str,
    command_id: str,
) -> None:
    response = client.get(
        f"{supabase_url}/rest/v1/manual_commands",
        headers=_headers(secret_key, secret_key),
        params={
            "select": "id,command_type,status,payload",
            "id": f"eq.{command_id}",
        },
    )
    _expect_status(response, 200, "verifier_command_cleanup_read_failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("verifier_command_cleanup_read_unexpected_response")
    row = rows[0]
    if not _is_verifier_command_row(row, command_id):
        raise RuntimeError("verifier_command_cleanup_identity_mismatch")
    status = row.get("status")
    if status == "applied":
        return
    if status not in {"pending", "accepted", "rejected"}:
        raise RuntimeError("verifier_command_cleanup_status_invalid")

    response = client.delete(
        f"{supabase_url}/rest/v1/manual_commands",
        headers=_headers(secret_key, secret_key, prefer="return=representation"),
        params={
            "id": f"eq.{command_id}",
            "status": f"eq.{status}",
            "select": "id,command_type,status,payload",
        },
    )
    _expect_status(response, 200, "verifier_command_cleanup_delete_failed")
    deleted_rows = response.json()
    if (
        not isinstance(deleted_rows, list)
        or len(deleted_rows) != 1
        or not _is_verifier_command_row(deleted_rows[0], command_id)
        or deleted_rows[0].get("status") != status
    ):
        raise RuntimeError("verifier_command_cleanup_delete_unexpected_response")


def _is_verifier_command_row(row: object, command_id: str) -> bool:
    if not isinstance(row, dict):
        return False
    payload = row.get("payload")
    return (
        row.get("id") == command_id
        and row.get("command_type") == "request_live_enable"
        and isinstance(payload, dict)
        and payload.get("provider_contract_version")
        == "hosted-live-enable-flow-verifier"
        and payload.get("risk_report_id") == "hosted-live-enable-flow-verifier"
        and payload.get("release_version") == "hosted-live-enable-flow-verifier"
    )


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
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("supabase_url_port_invalid") from exc
    if port not in {None, 443}:
        raise RuntimeError("supabase_url_must_use_default_https_port")
    _reject_non_hosted_hostname(parsed.hostname)
    return parsed.geturl().rstrip("/")


def _project_ref(value: str) -> str:
    normalized_url = _normalize_url(value)
    hostname = urlparse(normalized_url).hostname
    if hostname is None:
        raise RuntimeError("supabase_url_missing_host")
    return hostname.split(".", maxsplit=1)[0]


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

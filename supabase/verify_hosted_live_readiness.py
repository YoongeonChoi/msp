from __future__ import annotations

import argparse
import base64
import ipaddress
import math
import os
import re
import secrets
import socket
import ssl
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

EXPECTED_DENIED_STATUSES = {401, 403, 404}


@dataclass(frozen=True, slots=True)
class HostedSupabaseConfig:
    supabase_url: str
    publishable_key: str
    secret_key: str
    requester_jwt: str
    reviewer_jwt: str
    timeout_sec: float


@dataclass(frozen=True, slots=True)
class HostedVerificationResult:
    postgrest_root_ok: bool
    denied_rpc_count: int
    service_rpc_count: int
    denied_table_count: int
    service_table_count: int
    authenticated_table_count: int
    realtime_ok: bool


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = _parse_args(argv)
    env = environ or os.environ
    config, missing = _config_from_env(args, env)
    if missing:
        print("FINAL=SKIP hosted_supabase_env_missing missing=" + ",".join(missing))
        return 2

    assert config is not None
    try:
        with httpx.Client(timeout=config.timeout_sec) as client:
            result = run_checks(config, client=client)
    except Exception as exc:
        print("FINAL=FAIL hosted_supabase_live_readiness")
        print(_safe_error(exc, _config_secret_values(config)))
        return 1

    print(format_result(result))
    return 0


def run_checks(
    config: HostedSupabaseConfig,
    *,
    client: httpx.Client,
    realtime_probe: Callable[[HostedSupabaseConfig], bool] | None = None,
) -> HostedVerificationResult:
    _validate_config(config)
    supabase_url = _normalize_url(config.supabase_url)
    _check_postgrest_root(client, supabase_url, config.publishable_key)
    denied = 0
    denied += _expect_rpc_denied(
        client, supabase_url, config.publishable_key, "database_size_bytes", {}
    )
    denied += _expect_rpc_denied(
        client,
        supabase_url,
        config.publishable_key,
        "run_retention_cleanup",
        {"dry_run": True},
    )
    allowed = 0
    allowed += _expect_rpc_allowed(
        client, supabase_url, config.secret_key, "database_size_bytes", {}
    )
    allowed += _expect_rpc_allowed(
        client,
        supabase_url,
        config.secret_key,
        "run_retention_cleanup",
        {"dry_run": True},
    )
    denied_tables = 0
    denied_tables += _expect_table_denied(
        client,
        supabase_url,
        config.publishable_key,
        "bot_settings",
    )
    allowed_tables = 0
    allowed_tables += _expect_table_allowed(
        client,
        supabase_url,
        config.secret_key,
        "bot_settings",
    )
    authenticated_tables = 0
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
    authenticated_tables += _expect_authenticated_admin_role_read(
        client,
        supabase_url,
        config.publishable_key,
        config.requester_jwt,
        requester_id,
    )
    authenticated_tables += _expect_authenticated_admin_role_read(
        client,
        supabase_url,
        config.publishable_key,
        config.reviewer_jwt,
        reviewer_id,
    )
    realtime_ok = (realtime_probe or _check_realtime_handshake)(config)
    if not realtime_ok:
        raise RuntimeError("realtime_websocket_handshake_failed")
    return HostedVerificationResult(
        postgrest_root_ok=True,
        denied_rpc_count=denied,
        service_rpc_count=allowed,
        denied_table_count=denied_tables,
        service_table_count=allowed_tables,
        authenticated_table_count=authenticated_tables,
        realtime_ok=True,
    )


def format_result(result: HostedVerificationResult) -> str:
    return (
        "FINAL=PASS hosted_supabase_live_readiness "
        f"postgrest={int(result.postgrest_root_ok)} "
        f"anon_rpc_denied={result.denied_rpc_count} "
        f"service_rpc_allowed={result.service_rpc_count} "
        f"anon_table_denied={result.denied_table_count} "
        f"service_table_allowed={result.service_table_count} "
        f"authenticated_table_allowed={result.authenticated_table_count} "
        f"realtime={int(result.realtime_ok)}"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify hosted Supabase PostgREST RPC grants and Realtime WebSocket "
            "handshake without printing secrets."
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
) -> tuple[HostedSupabaseConfig | None, list[str]]:
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
        HostedSupabaseConfig(
            supabase_url=str(url),
            publishable_key=str(publishable_key),
            secret_key=str(secret_key),
            requester_jwt=str(requester_jwt),
            reviewer_jwt=str(reviewer_jwt),
            timeout_sec=float(args.timeout_sec),
        ),
        [],
    )


def _validate_config(config: HostedSupabaseConfig) -> None:
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


def _check_postgrest_root(client: httpx.Client, supabase_url: str, key: str) -> None:
    response = client.get(f"{supabase_url}/rest/v1/", headers=_headers(key))
    if response.status_code != 200:
        raise RuntimeError(f"postgrest_root_failed status={response.status_code}")


def _expect_rpc_denied(
    client: httpx.Client,
    supabase_url: str,
    key: str,
    rpc_name: str,
    payload: dict[str, object],
) -> int:
    response = client.post(
        f"{supabase_url}/rest/v1/rpc/{rpc_name}",
        headers=_headers(key),
        json=payload,
    )
    if response.status_code not in EXPECTED_DENIED_STATUSES:
        raise RuntimeError(f"{rpc_name}_anon_not_denied status={response.status_code}")
    return 1


def _expect_rpc_allowed(
    client: httpx.Client,
    supabase_url: str,
    key: str,
    rpc_name: str,
    payload: dict[str, object],
) -> int:
    response = client.post(
        f"{supabase_url}/rest/v1/rpc/{rpc_name}",
        headers=_headers(key),
        json=payload,
    )
    if response.status_code != 200:
        raise RuntimeError(f"{rpc_name}_service_role_failed status={response.status_code}")
    _ = response.json()
    return 1


def _expect_table_denied(
    client: httpx.Client,
    supabase_url: str,
    key: str,
    table_name: str,
) -> int:
    response = client.get(
        f"{supabase_url}/rest/v1/{table_name}?select=id&limit=1",
        headers=_headers(key),
    )
    if response.status_code not in EXPECTED_DENIED_STATUSES:
        raise RuntimeError(f"{table_name}_anon_table_not_denied status={response.status_code}")
    return 1


def _expect_table_allowed(
    client: httpx.Client,
    supabase_url: str,
    key: str,
    table_name: str,
) -> int:
    response = client.get(
        f"{supabase_url}/rest/v1/{table_name}?select=id&limit=1",
        headers=_headers(key),
    )
    if response.status_code != 200:
        raise RuntimeError(f"{table_name}_service_role_table_failed status={response.status_code}")
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"{table_name}_service_role_table_invalid_response")
    return 1


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
        headers=_headers(publishable_key, bearer=jwt),
    )
    if response.status_code != 200:
        raise RuntimeError(f"{label}_auth_user_failed status={response.status_code}")
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
        raise RuntimeError(f"{label}_auth_user_missing_id")
    return data["id"]


def _expect_authenticated_admin_role_read(
    client: httpx.Client,
    supabase_url: str,
    publishable_key: str,
    jwt: str,
    user_id: str,
) -> int:
    response = client.get(
        f"{supabase_url}/rest/v1/user_roles",
        headers=_headers(publishable_key, bearer=jwt),
        params={"select": "role", "user_id": f"eq.{user_id}"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"authenticated_user_roles_read_failed status={response.status_code}")
    rows = response.json()
    if not isinstance(rows, list) or not any(
        isinstance(row, dict) and row.get("role") == "admin" for row in rows
    ):
        raise RuntimeError("authenticated_admin_role_missing")
    return 1


def _headers(key: str, *, bearer: str | None = None) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {bearer or key}",
        "Content-Type": "application/json",
    }


def _check_realtime_handshake(config: HostedSupabaseConfig) -> bool:
    parsed = urlparse(_normalize_url(config.supabase_url))
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise RuntimeError("invalid_supabase_url")

    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    path = f"/realtime/v1/websocket?apikey={quote(config.publishable_key, safe='')}&vsn=1.0.0"
    request = "\r\n".join(
        [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "\r\n",
        ],
    ).encode("ascii")

    raw_socket = socket.create_connection((host, port), timeout=config.timeout_sec)
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            conn = context.wrap_socket(raw_socket, server_hostname=host)
        else:
            conn = raw_socket
        try:
            conn.settimeout(config.timeout_sec)
            conn.sendall(request)
            response = conn.recv(4096).decode("iso-8859-1", errors="replace")
        finally:
            conn.close()
    except Exception:
        raw_socket.close()
        raise

    status_line = response.split("\r\n", 1)[0]
    if " 101 " not in status_line:
        raise RuntimeError("realtime_websocket_handshake_failed status=" + status_line[:80])
    return True


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


def _config_secret_values(config: HostedSupabaseConfig) -> tuple[str, str, str, str]:
    return (
        config.publishable_key,
        config.secret_key,
        config.requester_jwt,
        config.reviewer_jwt,
    )


if __name__ == "__main__":
    sys.exit(main())

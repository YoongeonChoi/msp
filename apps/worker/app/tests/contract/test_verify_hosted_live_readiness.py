from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[5]
VERIFIER = ROOT / "supabase" / "verify_hosted_live_readiness.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_hosted_live_readiness", VERIFIER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hosted_verifier_skips_when_required_env_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _module()

    result = verifier.main([], environ={})

    output = capsys.readouterr().out
    assert result == 2
    assert "FINAL=SKIP hosted_supabase_env_missing" in output
    assert "SUPABASE_URL" in output
    assert "SUPABASE_SECRET_KEY" in output
    assert "SUPABASE_LIVE_REQUESTER_JWT" in output
    assert "SUPABASE_LIVE_REVIEWER_JWT" in output


def test_hosted_verifier_checks_rpc_grants_without_printing_secrets() -> None:
    verifier = _module()
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["apikey"]
        authorization = request.headers["authorization"]
        seen.append((request.method, request.url.path, key))
        if request.url.path == "/auth/v1/user":
            if authorization == "Bearer requester-jwt":
                return httpx.Response(200, json={"id": "requester-user-id"})
            if authorization == "Bearer reviewer-jwt":
                return httpx.Response(200, json={"id": "reviewer-user-id"})
            return httpx.Response(401, json={"message": "invalid jwt"})
        if request.url.path == "/rest/v1/user_roles":
            assert key == "publishable-test-key"
            if authorization == "Bearer requester-jwt":
                return httpx.Response(200, json=[{"role": "admin"}])
            if authorization == "Bearer reviewer-jwt":
                return httpx.Response(200, json=[{"role": "admin"}])
            return httpx.Response(403, json={"message": "permission denied"})
        assert authorization == f"Bearer {key}"
        if request.url.path == "/rest/v1/":
            return httpx.Response(200, json={"swagger": "2.0"})
        if request.url.path == "/rest/v1/bot_settings":
            if key == "publishable-test-key":
                return httpx.Response(403, json={"message": "permission denied"})
            return httpx.Response(200, json=[{"id": "singleton"}])
        if key == "publishable-test-key":
            return httpx.Response(403, json={"message": "permission denied"})
        if request.url.path.endswith("/database_size_bytes"):
            return httpx.Response(200, json=123456)
        if request.url.path.endswith("/run_retention_cleanup"):
            return httpx.Response(200, json={"worker_heartbeats": 0})
        return httpx.Response(500, json={"message": "unexpected"})

    config = verifier.HostedSupabaseConfig(
        supabase_url="https://project.supabase.co",
        publishable_key="publishable-test-key",
        secret_key="secret-test-key",
        requester_jwt="requester-jwt",
        reviewer_jwt="reviewer-jwt",
        timeout_sec=1.0,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verifier.run_checks(config, client=client, realtime_probe=lambda _config: True)

    output = verifier.format_result(result)
    assert result.denied_rpc_count == 2
    assert result.service_rpc_count == 2
    assert result.denied_table_count == 1
    assert result.service_table_count == 1
    assert result.authenticated_table_count == 2
    assert [line for line in output.splitlines() if line.strip()] == [
        "FINAL=PASS hosted_supabase_live_readiness "
        "postgrest=1 anon_rpc_denied=2 service_rpc_allowed=2 "
        "anon_table_denied=1 service_table_allowed=1 "
        "authenticated_table_allowed=2 realtime=1"
    ]
    assert "publishable-test-key" not in output
    assert "secret-test-key" not in output
    assert "requester-jwt" not in output
    assert "reviewer-jwt" not in output
    assert ("POST", "/rest/v1/rpc/database_size_bytes", "publishable-test-key") in seen
    assert ("POST", "/rest/v1/rpc/run_retention_cleanup", "secret-test-key") in seen
    assert ("GET", "/rest/v1/bot_settings", "publishable-test-key") in seen
    assert ("GET", "/rest/v1/bot_settings", "secret-test-key") in seen
    assert ("GET", "/auth/v1/user", "publishable-test-key") in seen
    assert ("GET", "/rest/v1/user_roles", "publishable-test-key") in seen


def test_hosted_verifier_rejects_reused_publishable_and_secret_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _module()

    result = verifier.main(
        [
            "--url",
            "https://project.supabase.co",
            "--publishable-key",
            "reused-secret-key",
            "--secret-key",
            "reused-secret-key",
            "--requester-jwt",
            "requester-jwt",
            "--reviewer-jwt",
            "reviewer-jwt",
        ],
        environ={},
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "FINAL=FAIL hosted_supabase_live_readiness" in output
    assert "supabase_publishable_and_secret_keys_must_be_distinct" in output
    assert "reused-secret-key" not in output


def test_hosted_verifier_rejects_non_positive_timeout() -> None:
    verifier = _module()
    config = verifier.HostedSupabaseConfig(
        supabase_url="https://project.supabase.co",
        publishable_key="publishable-test-key",
        secret_key="secret-test-key",
        requester_jwt="requester-jwt",
        reviewer_jwt="reviewer-jwt",
        timeout_sec=0.0,
    )

    with pytest.raises(RuntimeError, match="timeout_sec_must_be_positive"):
        verifier._validate_config(config)


def test_hosted_verifier_rejects_jwts_reusing_supabase_keys() -> None:
    verifier = _module()
    config = verifier.HostedSupabaseConfig(
        supabase_url="https://project.supabase.co",
        publishable_key="publishable-test-key",
        secret_key="secret-test-key",
        requester_jwt="publishable-test-key",
        reviewer_jwt="reviewer-jwt",
        timeout_sec=1.0,
    )

    with pytest.raises(RuntimeError, match="requester_jwt_must_not_reuse_supabase_key"):
        verifier._validate_config(config)


@pytest.mark.parametrize(
    ("url", "expected_error"),
    [
        ("http://project.supabase.co", "supabase_url_must_be_https"),
        ("https://localhost", "supabase_url_must_be_hosted"),
        ("https://127.0.0.1", "supabase_url_must_be_hosted"),
        ("https://project.test", "supabase_url_must_be_hosted"),
        ("https://example.com", "supabase_url_must_be_hosted_supabase_project"),
        ("https://user:pass@project.supabase.co", "supabase_url_must_not_include_credentials"),
        ("https://project.supabase.co/rest/v1", "supabase_url_must_not_include_path"),
        (
            "https://project.supabase.co?apikey=secret",
            "supabase_url_must_not_include_query_or_fragment",
        ),
    ],
)
def test_hosted_verifier_rejects_non_hosted_or_ambiguous_urls(
    url: str,
    expected_error: str,
) -> None:
    verifier = _module()

    with pytest.raises(RuntimeError, match=expected_error):
        verifier._normalize_url(url)


def test_hosted_verifier_redacts_secrets_from_failure_output() -> None:
    verifier = _module()

    message = verifier._safe_error(
        RuntimeError(
            "apikey=secret-test-key Authorization: Bearer requester-jwt "
            "Bearer reviewer-jwt sb_secret_abcd "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
        ),
        ("secret-test-key", "requester-jwt", "reviewer-jwt"),
    )

    assert "secret-test-key" not in message
    assert "requester-jwt" not in message
    assert "reviewer-jwt" not in message
    assert "sb_secret_abcd" not in message
    assert "eyJhbGciOiJIUzI1NiJ9" not in message
    assert "<redacted>" in message
    assert "<jwt_redacted>" in message


def test_hosted_verifier_uses_official_realtime_websocket_endpoint() -> None:
    source = VERIFIER.read_text(encoding="utf-8")

    assert "/realtime/v1/websocket" in source
    assert "apikey=" in source
    assert "Sec-WebSocket-Key" in source

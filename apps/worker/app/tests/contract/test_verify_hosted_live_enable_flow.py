from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[5]
VERIFIER = ROOT / "supabase" / "verify_hosted_live_enable_flow.py"

REQUESTER_ID = "11111111-1111-4111-8111-111111111111"
REVIEWER_ID = "22222222-2222-4222-8222-222222222222"


def _module() -> ModuleType:
    helper_dir = str(VERIFIER.parent)
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    spec = importlib.util.spec_from_file_location("verify_hosted_live_enable_flow", VERIFIER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hosted_live_enable_verifier_skips_when_required_env_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _module()

    result = verifier.main([], environ={})

    output = capsys.readouterr().out
    assert result == 2
    assert "FINAL=SKIP hosted_live_enable_env_missing" in output
    assert "SUPABASE_URL" in output
    assert "SUPABASE_LIVE_REQUESTER_JWT" in output
    assert "SUPABASE_LIVE_REVIEWER_JWT" in output


def test_hosted_live_enable_verifier_loads_explicit_env_file_without_printing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _module()
    env_file = tmp_path / ".env.hosted"
    env_file.write_text(
        "\n".join(
            [
                "SUPABASE_URL=https://project.supabase.co",
                "SUPABASE_PUBLISHABLE_KEY=publishable-test-key",
                "SUPABASE_SECRET_KEY=secret-test-key",
                "SUPABASE_LIVE_REQUESTER_JWT=requester-jwt",
                "SUPABASE_LIVE_REVIEWER_JWT=reviewer-jwt",
            ]
        ),
        encoding="utf-8",
    )

    def fake_run_checks(
        config: object,
        *,
        client: httpx.Client,
    ) -> object:
        assert isinstance(config, verifier.HostedLiveEnableConfig)
        assert config.supabase_url == "https://project.supabase.co"
        assert config.publishable_key == "publishable-test-key"
        assert config.secret_key == "secret-test-key"
        assert config.requester_jwt == "requester-jwt"
        assert config.reviewer_jwt == "reviewer-jwt"
        return verifier.HostedLiveEnableResult(
            requester_admin_ok=True,
            reviewer_admin_ok=True,
            request_created=True,
            self_review_denied=True,
            review_accepted=True,
            activation_consumed_once=True,
            second_activation_denied=True,
            command_id="command-id",
        )

    monkeypatch.setattr(verifier, "run_checks", fake_run_checks)

    result = verifier.main(["--env-file", str(env_file)], environ={})

    output = capsys.readouterr().out
    assert result == 0
    assert "FINAL=PASS hosted_live_enable_flow" in output
    assert "publishable-test-key" not in output
    assert "secret-test-key" not in output
    assert "requester-jwt" not in output
    assert "reviewer-jwt" not in output


def test_hosted_live_enable_verifier_env_file_failure_does_not_print_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _module()
    missing_env_file = tmp_path / "missing.env"

    result = verifier.main(["--env-file", str(missing_env_file)], environ={})

    output = capsys.readouterr().out
    assert result == 1
    assert "FINAL=FAIL hosted_live_enable_flow" in output
    assert "env_file_unreadable" in output
    assert str(missing_env_file) not in output


def test_hosted_live_enable_verifier_checks_user_flow_without_printing_secrets() -> None:
    verifier = _module()
    state = _MockHostedLiveEnableState()

    def handler(request: httpx.Request) -> httpx.Response:
        return state.handle(request)

    config = verifier.HostedLiveEnableConfig(
        supabase_url="https://project.supabase.co",
        publishable_key="publishable-test-key",
        secret_key="secret-test-key",
        requester_jwt="requester-jwt",
        reviewer_jwt="reviewer-jwt",
        timeout_sec=1.0,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verifier.run_checks(config, client=client)

    output = verifier.format_result(result)
    assert result.command_id == state.command_id
    assert state.self_review_attempted is True
    assert state.activation_count == 1
    assert state.second_activation_denied is True
    assert [line for line in output.splitlines() if line.strip()] == [
        "FINAL=PASS hosted_live_enable_flow "
        "requester_admin=1 reviewer_admin=1 request_created=1 "
        "self_review_denied=1 review_accepted=1 activation_consumed_once=1 "
        "second_activation_denied=1"
    ]
    assert "requester-jwt" not in output
    assert "reviewer-jwt" not in output
    assert "secret-test-key" not in output
    assert "publishable-test-key" not in output


def test_hosted_live_enable_verifier_fails_on_preexisting_unapplied_approval() -> None:
    verifier = _module()
    state = _MockHostedLiveEnableState(preexisting_unapplied=True)

    config = verifier.HostedLiveEnableConfig(
        supabase_url="https://project.supabase.co",
        publishable_key="publishable-test-key",
        secret_key="secret-test-key",
        requester_jwt="requester-jwt",
        reviewer_jwt="reviewer-jwt",
        timeout_sec=1.0,
    )
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as client:
        try:
            verifier.run_checks(config, client=client)
        except RuntimeError as exc:
            assert str(exc) == "preexisting_unapplied_live_enable_command"
        else:
            raise AssertionError("expected preexisting approval failure")


def test_hosted_live_enable_verifier_rejects_reused_requester_and_reviewer_jwt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _module()

    result = verifier.main(
        [
            "--url",
            "https://project.supabase.co",
            "--publishable-key",
            "publishable-test-key",
            "--secret-key",
            "secret-test-key",
            "--requester-jwt",
            "reused-admin-jwt",
            "--reviewer-jwt",
            "reused-admin-jwt",
        ],
        environ={},
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "FINAL=FAIL hosted_live_enable_flow" in output
    assert "requester_and_reviewer_jwts_must_be_distinct" in output
    assert "reused-admin-jwt" not in output


def test_hosted_live_enable_verifier_rejects_user_jwt_reusing_service_key() -> None:
    verifier = _module()
    config = verifier.HostedLiveEnableConfig(
        supabase_url="https://project.supabase.co",
        publishable_key="publishable-test-key",
        secret_key="secret-test-key",
        requester_jwt="secret-test-key",
        reviewer_jwt="reviewer-jwt",
        timeout_sec=1.0,
    )

    with pytest.raises(RuntimeError, match="requester_jwt_must_not_reuse_supabase_key"):
        verifier._validate_config(config)


def test_hosted_live_enable_verifier_rejects_reused_publishable_and_secret_key() -> None:
    verifier = _module()
    config = verifier.HostedLiveEnableConfig(
        supabase_url="https://project.supabase.co",
        publishable_key="same-key",
        secret_key="same-key",
        requester_jwt="requester-jwt",
        reviewer_jwt="reviewer-jwt",
        timeout_sec=1.0,
    )

    with pytest.raises(
        RuntimeError,
        match="supabase_publishable_and_secret_keys_must_be_distinct",
    ):
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
def test_hosted_live_enable_verifier_rejects_non_hosted_or_ambiguous_urls(
    url: str,
    expected_error: str,
) -> None:
    verifier = _module()

    with pytest.raises(RuntimeError, match=expected_error):
        verifier._normalize_url(url)


def test_hosted_live_enable_verifier_redacts_secrets_from_failure_output() -> None:
    verifier = _module()

    message = verifier._safe_error(
        RuntimeError(
            "apikey=secret-test-key Authorization: Bearer requester-jwt "
            "Bearer reviewer-jwt sb_secret_abcd "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
        ),
        ("publishable-test-key", "secret-test-key", "requester-jwt", "reviewer-jwt"),
    )

    assert "secret-test-key" not in message
    assert "requester-jwt" not in message
    assert "reviewer-jwt" not in message
    assert "sb_secret_abcd" not in message
    assert "eyJhbGciOiJIUzI1NiJ9" not in message
    assert "<redacted>" in message
    assert "<jwt_redacted>" in message


class _MockHostedLiveEnableState:
    def __init__(self, *, preexisting_unapplied: bool = False) -> None:
        self.preexisting_unapplied = preexisting_unapplied
        self.command_id = str(uuid4())
        self.command: dict[str, object] | None = None
        self.bot_settings = {
            "id": "singleton",
            "enabled": False,
            "mode": "paper",
            "live_order_allowed": False,
        }
        self.self_review_attempted = False
        self.activation_count = 0
        self.second_activation_denied = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").replace("Bearer ", "")
        if request.url.path == "/auth/v1/user" and request.method == "GET":
            return self._auth_user(token, request)
        if request.url.path == "/rest/v1/user_roles" and request.method == "GET":
            return httpx.Response(200, json=[{"role": "admin"}], request=request)
        if request.url.path == "/rest/v1/bot_settings" and request.method == "PATCH":
            return self._patch_bot_settings(request)
        if request.url.path == "/rest/v1/manual_commands" and request.method == "GET":
            return self._get_manual_commands(request)
        if request.url.path == "/rest/v1/manual_commands" and request.method == "POST":
            return self._post_manual_commands(request)
        if request.url.path == "/rest/v1/manual_commands" and request.method == "PATCH":
            return self._patch_manual_commands(token, request)
        return httpx.Response(
            500,
            json={"message": f"unexpected {request.method} {request.url}"},
            request=request,
        )

    def _auth_user(self, token: str, request: httpx.Request) -> httpx.Response:
        if token == "requester-jwt":
            return httpx.Response(200, json={"id": REQUESTER_ID}, request=request)
        if token == "reviewer-jwt":
            return httpx.Response(200, json={"id": REVIEWER_ID}, request=request)
        return httpx.Response(401, json={"message": "invalid token"}, request=request)

    def _patch_bot_settings(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        wants_live = (
            body.get("enabled") is True
            and body.get("mode") == "live"
            and body.get("live_order_allowed") is True
        )
        if wants_live:
            if self.command is None or self.command["status"] != "accepted":
                self.second_activation_denied = True
                return httpx.Response(
                    400,
                    json={"message": "live_order_allowed_requires_fresh_accepted_manual_command"},
                    request=request,
                )
            self.command["status"] = "applied"
            self.command["applied_at"] = datetime.now(UTC).isoformat()
            self.activation_count += 1
        self.bot_settings.update(body)
        prefer = request.headers.get("prefer", "")
        status_code = 200 if "return=representation" in prefer else 204
        json_body = [self.bot_settings] if status_code == 200 else None
        return httpx.Response(status_code, json=json_body, request=request)

    def _get_manual_commands(self, request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        if query.get("status") == "eq.accepted" and query.get("applied_at") == "is.null":
            if self.preexisting_unapplied:
                return httpx.Response(200, json=[{"id": str(uuid4())}], request=request)
            return httpx.Response(200, json=[], request=request)
        if query.get("id") == f"eq.{self.command_id}" and self.command is not None:
            return httpx.Response(200, json=[self.command], request=request)
        return httpx.Response(200, json=[], request=request)

    def _post_manual_commands(self, request: httpx.Request) -> httpx.Response:
        self.command = {
            "id": self.command_id,
            "command_type": "request_live_enable",
            "status": "pending",
            "requested_by": REQUESTER_ID,
            "reviewed_by": None,
            "reviewed_at": None,
            "applied_at": None,
            "expires_at": "2026-06-28T00:30:00+00:00",
            "payload": json.loads(request.content.decode("utf-8"))["payload"],
        }
        return httpx.Response(201, json=[self.command], request=request)

    def _patch_manual_commands(self, token: str, request: httpx.Request) -> httpx.Response:
        if self.command is None:
            return httpx.Response(404, json={"message": "missing"}, request=request)
        if token == "requester-jwt":
            self.self_review_attempted = True
            return httpx.Response(
                400,
                json={"message": "live_enable_self_review_forbidden"},
                request=request,
            )
        if token != "reviewer-jwt":
            return httpx.Response(403, json={"message": "permission denied"}, request=request)
        self.command["status"] = "accepted"
        self.command["reviewed_by"] = REVIEWER_ID
        self.command["reviewed_at"] = datetime.now(UTC).isoformat()
        self.command["applied_at"] = None
        return httpx.Response(200, json=[self.command], request=request)

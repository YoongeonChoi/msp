from __future__ import annotations

import httpx
import pytest

import app.tools.trigger_render_deploy_hook as deploy_hook

EXPECTED_SHA = "9ba5ec8b2929edcb318b48ca759eb36de9ebe8ea"
OTHER_SHA = "65550f95843824b1fae0f4ca61ef19fcaf694146"
HOOK_URL = "https://api.render.com/deploy/srv-test?key=super-secret-token"


def test_trigger_render_deploy_hook_posts_ref_without_printing_secret() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/deploy/srv-test"
        assert request.url.params["key"] == "super-secret-token"
        assert request.url.params["ref"] == EXPECTED_SHA
        return httpx.Response(200, json={"deploy": {"id": "dep-test"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = deploy_hook.trigger_render_deploy_hook(
        HOOK_URL,
        expected_sha=EXPECTED_SHA.upper(),
        client=client,
    )

    assert len(seen_requests) == 1
    assert result.expected_sha_short == EXPECTED_SHA[:12]
    assert result.status_code == 200
    assert deploy_hook._format_result(result) == (
        "FINAL=PASS render_deploy_hook "
        f"expected_sha_short={EXPECTED_SHA[:12]} status_code=200"
    )


def test_trigger_render_deploy_hook_replaces_existing_ref() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "super-secret-token"
        assert request.url.params["ref"] == EXPECTED_SHA
        assert OTHER_SHA not in str(request.url)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = deploy_hook.trigger_render_deploy_hook(
        HOOK_URL + f"&ref={OTHER_SHA}",
        expected_sha=EXPECTED_SHA,
        client=client,
    )

    assert result.status_code == 200


def test_cli_requires_confirmation_before_network_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    requested = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    exit_code = deploy_hook.main(
        ["--expected-sha", EXPECTED_SHA],
        environ={deploy_hook.RENDER_DEPLOY_HOOK_ENV: HOOK_URL},
        client=client,
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert requested is False
    assert output.strip() == (
        "FINAL=SKIP render_deploy_hook "
        f"reason=confirmation_required expected_sha_short={EXPECTED_SHA[:12]}"
    )
    assert "super-secret-token" not in output
    assert "api.render.com" not in output


def test_cli_skips_when_deploy_hook_env_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = deploy_hook.main(["--expected-sha", EXPECTED_SHA], environ={})

    output = capsys.readouterr().out
    assert exit_code == 2
    assert output.strip() == (
        "FINAL=SKIP render_deploy_hook "
        "reason=render_deploy_hook_env_missing missing=RENDER_DEPLOY_HOOK_URL "
        f"expected_sha_short={EXPECTED_SHA[:12]}"
    )


def test_cli_failure_does_not_print_hook_url_or_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="secret diagnostic body")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    exit_code = deploy_hook.main(
        ["--expected-sha", EXPECTED_SHA, "--yes"],
        environ={deploy_hook.RENDER_DEPLOY_HOOK_ENV: HOOK_URL},
        client=client,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL render_deploy_hook" in output
    assert "reason=render_deploy_hook_status_unexpected" in output
    assert EXPECTED_SHA not in output
    assert "super-secret-token" not in output
    assert "api.render.com" not in output
    assert "secret diagnostic body" not in output


def test_trigger_render_deploy_hook_rejects_non_render_urls() -> None:
    invalid_urls = [
        "http://api.render.com/deploy/srv-test?key=secret",
        "https://example.com/deploy/srv-test?key=secret",
        "https://user:pass@api.render.com/deploy/srv-test?key=secret",
        "https://api.render.com/not-deploy/srv-test?key=secret",
        "https://api.render.com/deploy/srv-test?key=secret#fragment",
    ]

    for url in invalid_urls:
        with pytest.raises(deploy_hook.RenderDeployHookError) as exc_info:
            deploy_hook.trigger_render_deploy_hook(url, expected_sha=EXPECTED_SHA)
        assert str(exc_info.value) == "render_deploy_hook_url_invalid"


def test_trigger_render_deploy_hook_rejects_invalid_sha() -> None:
    with pytest.raises(deploy_hook.RenderDeployHookError) as exc_info:
        deploy_hook.trigger_render_deploy_hook(HOOK_URL, expected_sha="abc123")

    assert str(exc_info.value) == "expected_sha_invalid"

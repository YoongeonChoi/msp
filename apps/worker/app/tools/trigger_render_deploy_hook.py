from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

GIT_SHA_RE = r"[A-Fa-f0-9]{12,64}"
RENDER_DEPLOY_HOOK_ENV = "RENDER_DEPLOY_HOOK_URL"


class RenderDeployHookError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RenderDeployHookResult:
    expected_sha_short: str
    status_code: int


def trigger_render_deploy_hook(
    hook_url: str,
    *,
    expected_sha: str,
    timeout_sec: float = 10.0,
    client: httpx.Client | None = None,
) -> RenderDeployHookResult:
    normalized_sha = _normalize_sha(expected_sha, "expected_sha")
    if not math.isfinite(timeout_sec) or timeout_sec <= 0:
        raise RenderDeployHookError("timeout_sec_must_be_positive")
    target_url = _render_deploy_hook_url_with_ref(hook_url, normalized_sha)
    close_client = client is None
    http_client = client or httpx.Client(timeout=timeout_sec)
    try:
        response = http_client.post(target_url)
    except httpx.HTTPError as exc:
        raise RenderDeployHookError("render_deploy_hook_unavailable") from exc
    finally:
        if close_client:
            http_client.close()
    if response.status_code != 200:
        raise RenderDeployHookError("render_deploy_hook_status_unexpected")
    return RenderDeployHookResult(
        expected_sha_short=_short_sha(normalized_sha),
        status_code=response.status_code,
    )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> int:
    args = _parse_args(argv)
    env = environ if environ is not None else os.environ
    try:
        expected_sha = args.expected_sha or _git_head(args.repo_root)
        normalized_sha = _normalize_sha(expected_sha, "expected_sha")
    except RenderDeployHookError as exc:
        print(f"FINAL=FAIL render_deploy_hook reason={exc}")
        return 1

    hook_url = args.hook_url or env.get(RENDER_DEPLOY_HOOK_ENV)
    if not hook_url:
        print(
            "FINAL=SKIP render_deploy_hook "
            f"reason=render_deploy_hook_env_missing missing={RENDER_DEPLOY_HOOK_ENV} "
            f"expected_sha_short={_short_sha(normalized_sha)}"
        )
        return 2
    if not args.yes:
        print(
            "FINAL=SKIP render_deploy_hook "
            f"reason=confirmation_required expected_sha_short={_short_sha(normalized_sha)}"
        )
        return 2

    try:
        result = trigger_render_deploy_hook(
            hook_url,
            expected_sha=normalized_sha,
            timeout_sec=args.timeout_sec,
            client=client,
        )
    except RenderDeployHookError as exc:
        print(
            "FINAL=FAIL render_deploy_hook "
            f"reason={exc} expected_sha_short={_short_sha(normalized_sha)}"
        )
        return 1

    print(_format_result(result))
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Trigger a manual Render deploy hook for the expected Git commit "
            "without printing the hook URL or secret query token."
        )
    )
    parser.add_argument("--hook-url", default=None, help="Render deploy hook URL")
    parser.add_argument(
        "--expected-sha",
        default=None,
        help="Expected Git commit SHA. Defaults to HEAD from --repo-root or cwd.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually call the deploy hook. Omit to validate configuration only.",
    )
    return parser.parse_args(argv)


def _format_result(result: RenderDeployHookResult) -> str:
    return (
        "FINAL=PASS render_deploy_hook "
        f"expected_sha_short={result.expected_sha_short} "
        f"status_code={result.status_code}"
    )


def _render_deploy_hook_url_with_ref(hook_url: str, expected_sha: str) -> str:
    normalized_sha = _normalize_sha(expected_sha, "expected_sha")
    parsed = urlparse(hook_url)
    if parsed.scheme != "https":
        raise RenderDeployHookError("render_deploy_hook_url_invalid")
    if parsed.hostname != "api.render.com":
        raise RenderDeployHookError("render_deploy_hook_url_invalid")
    if parsed.username or parsed.password:
        raise RenderDeployHookError("render_deploy_hook_url_invalid")
    if not parsed.path.startswith("/deploy/"):
        raise RenderDeployHookError("render_deploy_hook_url_invalid")
    if parsed.fragment:
        raise RenderDeployHookError("render_deploy_hook_url_invalid")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "ref"
    ]
    query_pairs.append(("ref", normalized_sha))
    return urlunparse(parsed._replace(query=urlencode(query_pairs)))


def _normalize_sha(value: str, field: str) -> str:
    if re.fullmatch(GIT_SHA_RE, value) is None:
        raise RenderDeployHookError(f"{field}_invalid")
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
        raise RenderDeployHookError("git_head_unavailable") from exc
    try:
        return output.decode("utf-8").strip()
    except UnicodeError as exc:
        raise RenderDeployHookError("git_head_unavailable") from exc


if __name__ == "__main__":
    raise SystemExit(main())

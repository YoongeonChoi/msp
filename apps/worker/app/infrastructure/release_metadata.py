from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

RELEASE_METADATA_PATH = Path(__file__).resolve().parents[1] / "release_metadata.json"
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
SOURCE_RE = re.compile(r"^[A-Z0-9_]{3,64}$")
_ENV_SHA_KEYS = (
    "APP_RELEASE_SHA",
    "GIT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
    "SOURCE_VERSION",
)


def worker_release_metadata(
    *,
    env: Mapping[str, str] | None = None,
    metadata_path: Path = RELEASE_METADATA_PATH,
) -> dict[str, object]:
    source_env = env if env is not None else os.environ
    env_metadata = _metadata_from_env(source_env)
    if env_metadata:
        return env_metadata
    return _metadata_from_file(metadata_path)


def worker_heartbeat_details(
    cycle_id: str,
    *,
    env: Mapping[str, str] | None = None,
    metadata_path: Path = RELEASE_METADATA_PATH,
) -> dict[str, object]:
    details: dict[str, object] = {"cycle_id": cycle_id}
    details.update(worker_release_metadata(env=env, metadata_path=metadata_path))
    return details


def _metadata_from_env(env: Mapping[str, str]) -> dict[str, object]:
    for key in _ENV_SHA_KEYS:
        value = _safe_sha(env.get(key))
        if value is not None:
            return {
                "release_sha": value,
                "release_sha_short": value[:12],
                "release_source": key,
            }
    return {}


def _metadata_from_file(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"release_source": "metadata_file_missing"}
    if not isinstance(payload, Mapping):
        return {"release_source": "metadata_file_invalid"}
    release_sha = _safe_sha(payload.get("release_sha"))
    release_source = _safe_source(payload.get("release_source"))
    if release_sha is None:
        return {"release_source": release_source or "metadata_file_sha_missing"}
    return {
        "release_sha": release_sha,
        "release_sha_short": release_sha[:12],
        "release_source": release_source or "metadata_file",
    }


def _safe_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if SHA_RE.fullmatch(candidate) is None:
        return None
    return candidate.lower()


def _safe_source(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if SOURCE_RE.fullmatch(candidate) is None:
        return None
    return candidate

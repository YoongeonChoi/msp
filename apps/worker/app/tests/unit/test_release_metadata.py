from __future__ import annotations

import json
from pathlib import Path

from app.infrastructure.release_metadata import (
    worker_heartbeat_details,
    worker_release_metadata,
)


def test_release_metadata_prefers_safe_env_sha(tmp_path: Path) -> None:
    metadata_file = tmp_path / "release_metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "release_sha": "1111111111111111111111111111111111111111",
                "release_source": "GIT_REV_PARSE",
            }
        ),
        encoding="utf-8",
    )

    metadata = worker_release_metadata(
        env={"APP_RELEASE_SHA": "ABCDEF1234567890"},
        metadata_path=metadata_file,
    )

    assert metadata == {
        "release_sha": "abcdef1234567890",
        "release_sha_short": "abcdef123456",
        "release_source": "APP_RELEASE_SHA",
    }


def test_release_metadata_falls_back_to_generated_file(tmp_path: Path) -> None:
    metadata_file = tmp_path / "release_metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "release_sha": "fedcba9876543210",
                "release_source": "GIT_REV_PARSE",
            }
        ),
        encoding="utf-8",
    )

    metadata = worker_release_metadata(env={}, metadata_path=metadata_file)

    assert metadata["release_sha"] == "fedcba9876543210"
    assert metadata["release_source"] == "GIT_REV_PARSE"


def test_release_metadata_rejects_secret_like_or_invalid_values(tmp_path: Path) -> None:
    metadata_file = tmp_path / "release_metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "release_sha": "sk-test-secret",
                "release_source": "client_secret",
            }
        ),
        encoding="utf-8",
    )

    metadata = worker_release_metadata(
        env={"APP_RELEASE_SHA": "Bearer secret-token"},
        metadata_path=metadata_file,
    )

    assert metadata == {"release_source": "metadata_file_sha_missing"}


def test_heartbeat_details_include_cycle_and_release(tmp_path: Path) -> None:
    details = worker_heartbeat_details(
        "cycle-1",
        env={"GIT_COMMIT_SHA": "1234567890abcdef"},
        metadata_path=tmp_path / "missing.json",
    )

    assert details["cycle_id"] == "cycle-1"
    assert details["release_sha"] == "1234567890abcdef"
    assert details["release_source"] == "GIT_COMMIT_SHA"

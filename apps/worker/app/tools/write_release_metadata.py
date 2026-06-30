from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.infrastructure.release_metadata import RELEASE_METADATA_PATH, _safe_sha


def build_release_metadata() -> dict[str, object]:
    release_sha = _git_output("rev-parse", "HEAD")
    metadata: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "release_source": "GIT_REV_PARSE",
    }
    safe_sha = _safe_sha(release_sha)
    if safe_sha is not None:
        metadata["release_sha"] = safe_sha
    else:
        metadata["release_source"] = "GIT_REV_PARSE_UNAVAILABLE"
    return metadata


def write_release_metadata(path: Path = RELEASE_METADATA_PATH) -> None:
    path.write_text(
        json.dumps(build_release_metadata(), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_output(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ("git", *args),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def main() -> None:
    write_release_metadata()


if __name__ == "__main__":
    main()

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path


class HostedEnvFileError(ValueError):
    pass


def merge_env_files(
    env_files: Sequence[Path],
    environ: Mapping[str, str],
) -> dict[str, str]:
    merged: dict[str, str] = {}
    for env_file in env_files:
        merged.update(_read_env_file(env_file))
    merged.update(environ)
    return merged


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HostedEnvFileError("env_file_unreadable") from exc

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
            continue
        values[key] = _unquote_env_value(value.strip())
    return values


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value

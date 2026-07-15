from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.domain.execution_v2.models import ExecutionInvariantError
from app.tests.unit.test_publish_paper_execution_source_v1 import (
    _candidate,
    _fixture,
)
from app.tools.publish_paper_execution_source_once import load_source_input


def test_load_source_input_requires_hash_and_strict_schema(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture["bars"] = [{"sequence": 1}]
    payload = {
        "schema_version": 1,
        "fencing_token": 7,
        "control_epoch": 3,
        "fixture": fixture,
        "candidate": _candidate(),
    }
    path = tmp_path / "source.json"
    raw = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(raw)

    loaded = load_source_input(path, expected_sha256=hashlib.sha256(raw).hexdigest())

    assert loaded.fencing_token == 7
    assert loaded.fixture["series_id"] == fixture["series_id"]


def test_load_source_input_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    raw = b'{"schema_version":1,"schema_version":1}'
    path = tmp_path / "duplicate.json"
    path.write_bytes(raw)

    with pytest.raises(ExecutionInvariantError, match="schema_is_invalid"):
        load_source_input(path, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_load_source_input_rejects_wrong_hash(tmp_path: Path) -> None:
    path = tmp_path / "source.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ExecutionInvariantError, match="sha256_mismatch"):
        load_source_input(path, expected_sha256="0" * 64)

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from app.adapters.persistence.supabase_paper_execution_producer import (
    SupabasePaperExecutionProducer,
)
from app.application.use_cases.publish_paper_execution_source_v1 import (
    PaperExecutionPublication,
    PublishPaperExecutionSourceV1,
)
from app.config import Settings, load_settings
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import ExecutionInvariantError

MAX_SOURCE_INPUT_BYTES = 2_000_000
MAX_SOURCE_BARS = 600
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PaperSourcePublicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    fencing_token: StrictInt = Field(gt=0)
    control_epoch: StrictInt = Field(gt=0)
    fixture: JsonObject
    candidate: JsonObject

    def validated_payloads(self) -> tuple[JsonObject, JsonObject]:
        bars = self.fixture.get("bars")
        if not isinstance(bars, list) or not 1 <= len(bars) <= MAX_SOURCE_BARS:
            raise ExecutionInvariantError("paper_source_input_bar_count_is_invalid")
        return self.fixture, self.candidate


def load_source_input(
    path: Path,
    *,
    expected_sha256: str,
) -> PaperSourcePublicationInput:
    normalized_sha256 = expected_sha256.strip().lower()
    if _SHA256_RE.fullmatch(normalized_sha256) is None:
        raise ExecutionInvariantError("paper_source_input_sha256_is_invalid")
    try:
        raw = _read_regular_file_once(path)
    except OSError as exc:
        raise ExecutionInvariantError("paper_source_input_file_is_unreadable") from exc
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, normalized_sha256):
        raise ExecutionInvariantError("paper_source_input_sha256_mismatch")
    try:
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        publication = PaperSourcePublicationInput.model_validate(decoded)
        publication.validated_payloads()
        return publication
    except (ValueError, TypeError, ValidationError) as exc:
        raise ExecutionInvariantError("paper_source_input_schema_is_invalid") from exc


async def run_once(
    settings: Settings,
    source_input: PaperSourcePublicationInput,
    *,
    now: datetime | None = None,
) -> PaperExecutionPublication:
    if not settings.execution_v2_paper_source_input_enabled:
        raise ExecutionInvariantError("paper_source_input_is_not_enabled")
    worker_id = settings.execution_v2_worker_id
    account_id = settings.execution_v2_account_id
    if worker_id is None or account_id is None:
        raise ExecutionInvariantError("paper_source_runtime_identity_is_missing")
    fixture, candidate = source_input.validated_payloads()
    producer = SupabasePaperExecutionProducer(
        settings,
        account_id=account_id,
    )
    try:
        return await PublishPaperExecutionSourceV1(
            producer,
            account_id=account_id,
            worker_id=worker_id,
        ).publish(
            fixture=fixture,
            candidate=candidate,
            fencing_token=source_input.fencing_token,
            control_epoch=source_input.control_epoch,
            now=now or datetime.now(UTC),
        )
    finally:
        await producer.close()


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish one hash-pinned Paper bar fixture and candidate through "
            "the fenced worker_api RPC boundary."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        source_input = load_source_input(
            args.input,
            expected_sha256=args.input_sha256,
        )
        publication = await run_once(load_settings(), source_input)
    except ExecutionInvariantError as exc:
        print(
            "FINAL=FAIL paper_execution_source_publish "
            f"reason_code={exc.safe_message} production_order_network_requests=0",
            flush=True,
        )
        return 1
    except ValidationError:
        print(
            "FINAL=FAIL paper_execution_source_publish "
            "reason_code=paper_source_runtime_configuration_is_invalid "
            "production_order_network_requests=0",
            flush=True,
        )
        return 1
    print(
        "FINAL=PASS paper_execution_source_publish "
        f"fixture_set_id={publication.fixture.fixture_set_id} "
        f"intent_id={publication.candidate.intent_id} "
        f"command_id={publication.candidate.command_id} "
        f"fixture_idempotent={str(publication.fixture.idempotent).lower()} "
        f"candidate_idempotent={str(publication.candidate.idempotent).lower()} "
        "production_order_network_requests=0",
        flush=True,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _read_regular_file_once(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        path_stat = os.stat(path, follow_symlinks=False)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        is_reparse_point = bool(
            getattr(path_stat, "st_file_attributes", 0) & reparse_attribute
        )
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or is_reparse_point
            or not os.path.samestat(opened_stat, path_stat)
        ):
            raise ExecutionInvariantError("paper_source_input_file_is_invalid")
        if opened_stat.st_size > MAX_SOURCE_INPUT_BYTES:
            raise ExecutionInvariantError("paper_source_input_file_is_too_large")
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_SOURCE_INPUT_BYTES:
            raise ExecutionInvariantError("paper_source_input_file_is_too_large")
        return raw
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())

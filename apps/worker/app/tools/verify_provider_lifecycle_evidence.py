from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, unquote, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

DRILL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,120}$")
SYMBOL_RE = re.compile(r"^\d{6}$")
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
PERCENT_ENCODED_RE = re.compile(r"%[0-9A-Fa-f]{2}")
OPERATOR_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,80}$")
URI_UNRESERVED_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
SENSITIVE_KEY_RE = re.compile(
    r"(authorization|secret|client_secret|access_token|refresh_token|"
    r"api[_-]?key|token|password|account_number|account_no|acct_no)",
    re.IGNORECASE,
)
REQUIRED_ARTIFACT_TYPES = {
    "broker_order_receipt",
    "provider_status_export",
    "cancel_confirmation",
    "unknown_recovery_review",
    "repository_audit_export",
}
MAX_FUTURE_EVIDENCE_SKEW_SECONDS = 5 * 60
REMOTE_ARTIFACT_TIMEOUT_SECONDS = 10
MAX_REMOTE_ARTIFACT_BYTES = 5_000_000
BLOCKED_ARTIFACT_URI_TERMS = (
    "mock",
    "sample",
    "fixture",
    "localhost",
    "127.0.0.1",
    "file://",
    "/tmp/",
    "\\tmp\\",
    "example.com",
)
PRIVATE_RETAINED_DNS_SUFFIXES = (
    ".corp",
    ".home",
    ".internal",
    ".intranet",
    ".lan",
    ".private",
)
OPERATOR_REVIEW_BLOCKED_TERMS = (
    "automation",
    "bot",
    "ci",
    "github-actions",
    "script",
    "service-account",
    "system",
)

CREATE_LOCAL_STATUSES = {
    "sent",
    "partial_filled",
    "filled",
    "unknown_requires_manual_check",
}
LOCAL_STATUSES = {
    "sent",
    "partial_filled",
    "filled",
    "canceled",
    "rejected",
    "unknown_requires_manual_check",
}
PROVIDER_TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
PROVIDER_IRREVERSIBLE_TERMINAL_STATUSES = {"FILLED", "REJECTED", "EXPIRED"}
ALLOWED_PROVIDER_STATUSES = {
    "PENDING",
    "PENDING_CANCEL",
    "PENDING_REPLACE",
    "PARTIAL_FILLED",
    "FILLED",
    "CANCELED",
    "REJECTED",
    "EXPIRED",
}
LOCAL_TERMINAL_STATUSES = {"filled", "canceled", "rejected", "unknown_requires_manual_check"}
MATCHING_TERMINAL_STATUS_PAIRS = {
    "FILLED": {"filled"},
    "CANCELED": {"canceled"},
    "REJECTED": {"rejected", "unknown_requires_manual_check"},
    "EXPIRED": {"rejected", "unknown_requires_manual_check"},
}
UNKNOWN_RECOVERY_FINAL_STATUSES = {
    "filled",
    "canceled",
    "rejected",
    "unknown_requires_manual_check",
}
AUDIT_MINIMUMS = {
    "orders_reviewed": 3,
    "engine_events_reviewed": 2,
    "audit_logs_reviewed": 2,
}
EVIDENCE_ROOT_KEYS = {
    "schema_version",
    "drill_id",
    "provider",
    "environment",
    "started_at",
    "completed_at",
    "live_order_allowed_before",
    "live_order_allowed_after",
    "created_order",
    "provider_status_sequence",
    "cancel_probe",
    "unknown_recovery",
    "audit",
    "evidence_artifacts",
}
CREATED_ORDER_KEYS = {
    "local_order_id",
    "created_at",
    "symbol",
    "action",
    "order_type",
    "provider_order_id_redacted",
    "amount_krw",
    "status_after_create",
}
STATUS_OBSERVATION_KEYS = {
    "observed_at",
    "local_order_id",
    "provider_status",
    "local_status",
}
CANCEL_PROBE_KEYS = {
    "attempted",
    "attempted_at",
    "local_order_id",
    "provider_cancel_id_redacted",
    "provider_final_status",
    "local_status",
}
UNKNOWN_RECOVERY_KEYS = {
    "order_id",
    "reason",
    "engine_event_message",
    "operator_reviewed_at",
    "operator_reviewed_by",
    "final_status",
}
AUDIT_KEYS = set(AUDIT_MINIMUMS) | {"reviewed_by", "reviewed_at"}
EVIDENCE_ARTIFACT_KEYS = {"type", "drill_id", "uri", "sha256", "captured_at"}
REDACTED_IDENTIFIER_RE = re.compile(
    r"(?:redacted:[0-9a-f]{12,64}|[a-z][a-z0-9_]{1,40}_\.\.\.[0-9a-f]{4,12})"
)


class EvidenceValidationError(ValueError):
    pass


RemoteArtifactFetcher = Callable[[str, int], bytes]


@dataclass(frozen=True, slots=True)
class ProviderLifecycleEvidenceSummary:
    provider: str
    environment: str
    status_observations: int
    audit_logs_reviewed: int
    evidence_artifacts: int


@dataclass(frozen=True, slots=True)
class StatusSequenceSummary:
    latest_observed_at: datetime | None
    first_local_status: str | None
    latest_local_status: str | None
    first_canceled_pair_observed_at: datetime | None
    latest_canceled_pair_observed_at: datetime | None
    first_irreversible_terminal_observed_at: datetime | None


def verify_provider_lifecycle_evidence_file(
    path: Path,
    *,
    verify_remote_artifacts: bool = False,
    remote_fetcher: RemoteArtifactFetcher | None = None,
    remote_timeout_seconds: int = REMOTE_ARTIFACT_TIMEOUT_SECONDS,
) -> ProviderLifecycleEvidenceSummary:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvidenceValidationError("evidence_file_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceValidationError("evidence_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise EvidenceValidationError("evidence_root_must_be_object")
    evidence = cast(Mapping[str, object], payload)
    summary = verify_provider_lifecycle_evidence(evidence)
    if verify_remote_artifacts:
        verify_provider_lifecycle_remote_artifacts(
            evidence,
            fetcher=remote_fetcher,
            timeout_seconds=remote_timeout_seconds,
        )
    return summary


def verify_provider_lifecycle_evidence(
    payload: Mapping[str, object],
) -> ProviderLifecycleEvidenceSummary:
    errors: list[str] = []
    _scan_for_sensitive_keys(payload, "evidence", errors)
    _reject_unknown_keys(payload, EVIDENCE_ROOT_KEYS, "evidence", errors)

    if payload.get("schema_version") != 1:
        errors.append("schema_version_must_be_1")

    provider = _require_str(payload, "provider", "evidence", errors)
    if provider is not None and provider != "toss":
        errors.append("provider_must_be_toss")

    environment = _require_str(payload, "environment", "evidence", errors)
    if environment is not None and environment not in {"sandbox", "live"}:
        errors.append("environment_must_be_sandbox_or_live")

    drill_id = _require_str(payload, "drill_id", "evidence", errors)
    if drill_id is not None and DRILL_ID_RE.fullmatch(drill_id) is None:
        errors.append("drill_id_invalid")

    started_at = _require_timestamp(payload, "started_at", "evidence", errors)
    completed_at = _require_timestamp(payload, "completed_at", "evidence", errors)
    _require_not_future(started_at, "evidence.started_at", errors)
    _require_not_future(completed_at, "evidence.completed_at", errors)
    if started_at is not None and completed_at is not None and completed_at <= started_at:
        errors.append("completed_at_must_be_after_started_at")

    live_before = _require_bool(payload, "live_order_allowed_before", "evidence", errors)
    if live_before is not None and live_before is not False:
        errors.append("live_order_allowed_before_must_be_false")
    live_after = _require_bool(payload, "live_order_allowed_after", "evidence", errors)
    if live_after is not None and live_after is not False:
        errors.append("live_order_allowed_after_must_be_false")

    created_order = _require_mapping(payload, "created_order", "evidence", errors)
    created_order_id: str | None = None
    created_order_created_at: datetime | None = None
    created_order_status_after_create: str | None = None
    if created_order is not None:
        (
            created_order_id,
            created_order_created_at,
            created_order_status_after_create,
        ) = _validate_created_order(
            created_order,
            started_at,
            completed_at,
            errors,
        )

    status_sequence = _require_list(payload, "provider_status_sequence", "evidence", errors)
    status_summary = StatusSequenceSummary(
        latest_observed_at=None,
        first_local_status=None,
        latest_local_status=None,
        first_canceled_pair_observed_at=None,
        latest_canceled_pair_observed_at=None,
        first_irreversible_terminal_observed_at=None,
    )
    if status_sequence is not None:
        status_summary = _validate_status_sequence(
            status_sequence,
            created_order_id,
            created_order_created_at,
            started_at,
            completed_at,
            errors,
        )
    _validate_created_order_status_sequence_binding(
        created_order_status_after_create,
        status_summary,
        errors,
    )

    cancel_probe = _require_mapping(payload, "cancel_probe", "evidence", errors)
    cancel_probe_attempted_at: datetime | None = None
    if cancel_probe is not None:
        cancel_probe_attempted_at = _validate_cancel_probe(
            cancel_probe,
            created_order_id,
            created_order_created_at,
            started_at,
            completed_at,
            errors,
        )
        _validate_cancel_probe_status_sequence_binding(
            cancel_probe_attempted_at,
            status_summary,
            errors,
        )

    unknown_recovery = _require_mapping(payload, "unknown_recovery", "evidence", errors)
    unknown_recovery_reviewed_at: datetime | None = None
    unknown_recovery_reviewed_by: str | None = None
    unknown_recovery_final_status: str | None = None
    if unknown_recovery is not None:
        (
            unknown_recovery_reviewed_at,
            unknown_recovery_reviewed_by,
            unknown_recovery_final_status,
        ) = _validate_unknown_recovery(
            unknown_recovery,
            created_order_id,
            started_at,
            completed_at,
            errors,
        )
    _validate_unknown_recovery_review_sequence(
        status_summary,
        unknown_recovery_reviewed_at,
        errors,
    )
    _validate_unknown_recovery_final_status_sequence(
        status_summary,
        unknown_recovery_final_status,
        errors,
    )

    audit = _require_mapping(payload, "audit", "evidence", errors)
    audit_logs_reviewed: int | None = None
    audit_reviewed_at: datetime | None = None
    audit_reviewed_by: str | None = None
    if audit is not None:
        audit_logs_reviewed, audit_reviewed_at, audit_reviewed_by = _validate_audit(
            audit,
            started_at,
            completed_at,
            errors,
        )
    _validate_provider_reviewer_independence(
        unknown_recovery_reviewed_by,
        audit_reviewed_by,
        errors,
    )
    _validate_provider_review_sequence(
        unknown_recovery_reviewed_at,
        audit_reviewed_at,
        errors,
    )

    evidence_artifacts = _require_list(payload, "evidence_artifacts", "evidence", errors)
    evidence_artifact_count = 0
    if evidence_artifacts is not None:
        evidence_artifact_count = _validate_evidence_artifacts(
            evidence_artifacts,
            drill_id,
            started_at,
            completed_at,
            artifact_event_anchors={
                "broker_order_receipt": created_order_created_at,
                "provider_status_export": status_summary.latest_observed_at,
                "cancel_confirmation": status_summary.latest_canceled_pair_observed_at,
                "unknown_recovery_review": unknown_recovery_reviewed_at,
                "repository_audit_export": audit_reviewed_at,
            },
            errors=errors,
        )

    if errors:
        raise EvidenceValidationError(_join_errors(errors))

    assert provider is not None
    assert environment is not None
    assert status_sequence is not None
    assert audit_logs_reviewed is not None
    return ProviderLifecycleEvidenceSummary(
        provider=provider,
        environment=environment,
        status_observations=len(status_sequence),
        audit_logs_reviewed=audit_logs_reviewed,
        evidence_artifacts=evidence_artifact_count,
    )


def verify_provider_lifecycle_remote_artifacts(
    payload: Mapping[str, object],
    *,
    fetcher: RemoteArtifactFetcher | None = None,
    timeout_seconds: int = REMOTE_ARTIFACT_TIMEOUT_SECONDS,
) -> None:
    errors: list[str] = []
    _validate_evidence_artifacts_remote_fetch(
        payload,
        fetcher=fetcher or _default_remote_artifact_fetcher,
        timeout_seconds=timeout_seconds,
        errors=errors,
    )
    if errors:
        raise EvidenceValidationError(_join_errors(errors))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate redacted provider sandbox/live order lifecycle evidence."
    )
    parser.add_argument(
        "--evidence",
        required=True,
        type=Path,
        help="Path to the redacted provider lifecycle evidence JSON file.",
    )
    parser.add_argument(
        "--verify-remote-artifacts",
        action="store_true",
        help=(
            "Fetch every evidence_artifacts[].uri over HTTPS and require the "
            "downloaded bytes to match evidence_artifacts[].sha256. Use this "
            "after publishing release evidence."
        ),
    )
    args = parser.parse_args(argv)

    try:
        summary = verify_provider_lifecycle_evidence_file(
            args.evidence,
            verify_remote_artifacts=args.verify_remote_artifacts,
        )
    except EvidenceValidationError as exc:
        print(f"FINAL=FAIL provider_lifecycle_evidence reason={_safe_reason(str(exc))}")
        return 1

    print(
        "FINAL=PASS provider_lifecycle_evidence "
        f"provider={summary.provider} "
        f"environment={summary.environment} "
        f"status_observations={summary.status_observations} "
        f"audit_logs_reviewed={summary.audit_logs_reviewed} "
        f"evidence_artifacts={summary.evidence_artifacts}"
    )
    return 0


def _validate_created_order(
    order: Mapping[str, object],
    started_at: datetime | None,
    completed_at: datetime | None,
    errors: list[str],
) -> tuple[str | None, datetime | None, str | None]:
    _reject_unknown_keys(order, CREATED_ORDER_KEYS, "created_order", errors)
    local_order_id = _require_str(order, "local_order_id", "created_order", errors)
    _validate_uuid(local_order_id, "created_order.local_order_id", errors)

    created_at = _require_timestamp(order, "created_at", "created_order", errors)
    _require_inside_window(
        created_at,
        started_at,
        completed_at,
        "created_order.created_at",
        errors,
    )

    symbol = _require_str(order, "symbol", "created_order", errors)
    if symbol is not None and SYMBOL_RE.fullmatch(symbol) is None:
        errors.append("created_order.symbol_must_be_krx_code")

    action = _require_str(order, "action", "created_order", errors)
    if action is not None and action not in {"buy", "sell"}:
        errors.append("created_order.action_must_be_buy_or_sell")

    _require_str(order, "order_type", "created_order", errors)
    _require_redacted_identifier(order, "provider_order_id_redacted", "created_order", errors)

    amount_krw = _require_number(order, "amount_krw", "created_order", errors)
    if amount_krw is not None and amount_krw <= 0:
        errors.append("created_order.amount_krw_must_be_positive")

    status = _require_str(order, "status_after_create", "created_order", errors)
    if status is not None and status not in CREATE_LOCAL_STATUSES:
        errors.append("created_order.status_after_create_invalid")
    return local_order_id, created_at, status


def _validate_status_sequence(
    sequence: Sequence[object],
    created_order_id: str | None,
    created_order_created_at: datetime | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    errors: list[str],
) -> StatusSequenceSummary:
    if len(sequence) < 2:
        errors.append("provider_status_sequence_requires_at_least_2_observations")
        return StatusSequenceSummary(
            latest_observed_at=None,
            first_local_status=None,
            latest_local_status=None,
            first_canceled_pair_observed_at=None,
            latest_canceled_pair_observed_at=None,
            first_irreversible_terminal_observed_at=None,
        )

    has_terminal_provider = False
    has_terminal_local = False
    has_matching_terminal_pair = False
    previous_observed_at: datetime | None = None
    first_canceled_pair_observed_at: datetime | None = None
    latest_canceled_pair_observed_at: datetime | None = None
    first_irreversible_terminal_observed_at: datetime | None = None
    first_terminal_provider_status: str | None = None
    first_terminal_local_status: str | None = None
    first_local_status: str | None = None
    latest_local_status: str | None = None
    for index, item in enumerate(sequence):
        path = f"provider_status_sequence[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path}_must_be_object")
            continue
        observation = cast(Mapping[str, object], item)
        _reject_unknown_keys(observation, STATUS_OBSERVATION_KEYS, path, errors)
        observed_at = _require_timestamp(observation, "observed_at", path, errors)
        _require_inside_window(observed_at, started_at, completed_at, f"{path}.observed_at", errors)
        if (
            observed_at is not None
            and created_order_created_at is not None
            and observed_at <= created_order_created_at
        ):
            errors.append(f"{path}.observed_at_must_be_after_created_order")
        if (
            observed_at is not None
            and previous_observed_at is not None
            and observed_at <= previous_observed_at
        ):
            errors.append(f"{path}.observed_at_must_be_after_previous_observation")
        if observed_at is not None:
            previous_observed_at = observed_at
        local_order_id = _require_str(observation, "local_order_id", path, errors)
        _validate_uuid(local_order_id, f"{path}.local_order_id", errors)
        if (
            local_order_id is not None
            and created_order_id is not None
            and local_order_id != created_order_id
        ):
            errors.append(f"{path}.local_order_id_must_match_created_order")

        provider_status = _require_str(observation, "provider_status", path, errors)
        normalized_provider_status: str | None = None
        if provider_status is not None:
            normalized_provider_status = provider_status.strip().upper()
            if provider_status != normalized_provider_status:
                errors.append(f"{path}.provider_status_must_be_uppercase_known_status")
            if normalized_provider_status not in ALLOWED_PROVIDER_STATUSES:
                errors.append(f"{path}.provider_status_unknown")
            if normalized_provider_status in PROVIDER_TERMINAL_STATUSES:
                if first_terminal_provider_status is None:
                    first_terminal_provider_status = normalized_provider_status
                elif normalized_provider_status != first_terminal_provider_status:
                    errors.append(
                        f"{path}.provider_status_must_not_change_after_terminal"
                    )
            elif first_terminal_provider_status is not None:
                errors.append(
                    f"{path}.provider_status_must_not_regress_after_terminal"
                )
            has_terminal_provider = has_terminal_provider or (
                normalized_provider_status in PROVIDER_TERMINAL_STATUSES
            )
            if (
                normalized_provider_status in PROVIDER_IRREVERSIBLE_TERMINAL_STATUSES
                and observed_at is not None
                and first_irreversible_terminal_observed_at is None
            ):
                first_irreversible_terminal_observed_at = observed_at

        local_status = _require_str(observation, "local_status", path, errors)
        if local_status is not None:
            if local_status not in LOCAL_STATUSES:
                errors.append(f"{path}.local_status_invalid")
            else:
                if first_local_status is None:
                    first_local_status = local_status
                latest_local_status = local_status
            if local_status in LOCAL_TERMINAL_STATUSES:
                if first_terminal_local_status is None:
                    first_terminal_local_status = local_status
                elif local_status != first_terminal_local_status:
                    errors.append(f"{path}.local_status_must_not_change_after_terminal")
            elif first_terminal_local_status is not None:
                errors.append(f"{path}.local_status_must_not_regress_after_terminal")
            has_terminal_local = has_terminal_local or local_status in LOCAL_TERMINAL_STATUSES
            if (
                normalized_provider_status is not None
                and local_status
                in MATCHING_TERMINAL_STATUS_PAIRS.get(normalized_provider_status, set())
            ):
                has_matching_terminal_pair = True
                if (
                    normalized_provider_status == "CANCELED"
                    and local_status == "canceled"
                    and observed_at is not None
                ):
                    if first_canceled_pair_observed_at is None:
                        first_canceled_pair_observed_at = observed_at
                    latest_canceled_pair_observed_at = observed_at

    if not has_terminal_provider:
        errors.append("provider_status_sequence_missing_terminal_provider_status")
    if not has_terminal_local:
        errors.append("provider_status_sequence_missing_terminal_local_status")
    if not has_matching_terminal_pair:
        errors.append("provider_status_sequence_missing_matching_terminal_status_pair")
    return StatusSequenceSummary(
        latest_observed_at=previous_observed_at,
        first_local_status=first_local_status,
        latest_local_status=latest_local_status,
        first_canceled_pair_observed_at=first_canceled_pair_observed_at,
        latest_canceled_pair_observed_at=latest_canceled_pair_observed_at,
        first_irreversible_terminal_observed_at=first_irreversible_terminal_observed_at,
    )


def _validate_created_order_status_sequence_binding(
    status_after_create: str | None,
    status_summary: StatusSequenceSummary,
    errors: list[str],
) -> None:
    first_local_status = status_summary.first_local_status
    if (
        status_after_create is None
        or first_local_status is None
        or status_after_create == first_local_status
    ):
        return
    errors.append("created_order.status_after_create_must_match_first_local_status")


def _validate_cancel_probe(
    cancel_probe: Mapping[str, object],
    created_order_id: str | None,
    created_order_created_at: datetime | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    errors: list[str],
) -> datetime | None:
    _reject_unknown_keys(cancel_probe, CANCEL_PROBE_KEYS, "cancel_probe", errors)
    attempted = _require_bool(cancel_probe, "attempted", "cancel_probe", errors)
    if attempted is not None and attempted is not True:
        errors.append("cancel_probe.attempted_must_be_true")
    attempted_at = _require_timestamp(cancel_probe, "attempted_at", "cancel_probe", errors)
    _require_inside_window(
        attempted_at,
        started_at,
        completed_at,
        "cancel_probe.attempted_at",
        errors,
    )
    if (
        attempted_at is not None
        and created_order_created_at is not None
        and attempted_at <= created_order_created_at
    ):
        errors.append("cancel_probe.attempted_at_must_be_after_created_order")
    local_order_id = _require_str(cancel_probe, "local_order_id", "cancel_probe", errors)
    _validate_uuid(local_order_id, "cancel_probe.local_order_id", errors)
    if (
        local_order_id is not None
        and created_order_id is not None
        and local_order_id != created_order_id
    ):
        errors.append("cancel_probe.local_order_id_must_match_created_order")
    _require_redacted_identifier(
        cancel_probe,
        "provider_cancel_id_redacted",
        "cancel_probe",
        errors,
    )

    provider_final_status = _require_str(
        cancel_probe, "provider_final_status", "cancel_probe", errors
    )
    if provider_final_status is not None and provider_final_status != "CANCELED":
        errors.append("cancel_probe.provider_final_status_must_be_uppercase_canceled")

    local_status = _require_str(cancel_probe, "local_status", "cancel_probe", errors)
    if local_status is not None and local_status != "canceled":
        errors.append("cancel_probe.local_status_must_be_canceled")
    return attempted_at


def _validate_cancel_probe_status_sequence_binding(
    attempted_at: datetime | None,
    status_summary: StatusSequenceSummary,
    errors: list[str],
) -> None:
    latest_canceled_at = status_summary.latest_canceled_pair_observed_at
    if latest_canceled_at is None:
        errors.append("cancel_probe.missing_canceled_status_observation")
        return
    if attempted_at is None:
        return
    first_canceled_at = status_summary.first_canceled_pair_observed_at
    first_canceled_before_or_at_attempt = (
        first_canceled_at is not None and first_canceled_at <= attempted_at
    )
    latest_canceled_not_after_attempt = latest_canceled_at <= attempted_at
    if first_canceled_before_or_at_attempt or latest_canceled_not_after_attempt:
        errors.append("cancel_probe.canceled_status_observed_before_attempt")
    irreversible_terminal_at = status_summary.first_irreversible_terminal_observed_at
    if irreversible_terminal_at is not None and irreversible_terminal_at <= attempted_at:
        errors.append(
            "cancel_probe.irreversible_terminal_status_observed_before_attempt"
        )


def _validate_unknown_recovery(
    recovery: Mapping[str, object],
    created_order_id: str | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    errors: list[str],
) -> tuple[datetime | None, str | None, str | None]:
    _reject_unknown_keys(recovery, UNKNOWN_RECOVERY_KEYS, "unknown_recovery", errors)
    order_id = _require_str(recovery, "order_id", "unknown_recovery", errors)
    _validate_uuid(order_id, "unknown_recovery.order_id", errors)
    if order_id is not None and created_order_id is not None and order_id != created_order_id:
        errors.append("unknown_recovery.order_id_must_match_created_order")
    _require_str(recovery, "reason", "unknown_recovery", errors)

    message = _require_str(recovery, "engine_event_message", "unknown_recovery", errors)
    if message is not None and message != "live_order_manual_check_still_unknown":
        errors.append("unknown_recovery.engine_event_message_invalid")

    reviewed_at = _require_timestamp(recovery, "operator_reviewed_at", "unknown_recovery", errors)
    _require_inside_window(
        reviewed_at,
        started_at,
        completed_at,
        "unknown_recovery.operator_reviewed_at",
        errors,
    )
    operator_reviewed_by = _require_str(
        recovery, "operator_reviewed_by", "unknown_recovery", errors
    )
    if operator_reviewed_by is not None and _contains_blocked_operator_identity_segment(
        operator_reviewed_by
    ):
        errors.append("unknown_recovery.operator_reviewed_by_must_be_human")
    if (
        operator_reviewed_by is not None
        and OPERATOR_HANDLE_RE.fullmatch(operator_reviewed_by) is None
    ):
        errors.append("unknown_recovery.operator_reviewed_by_must_be_logical_operator_id")

    final_status = _require_str(recovery, "final_status", "unknown_recovery", errors)
    if final_status is not None and final_status not in UNKNOWN_RECOVERY_FINAL_STATUSES:
        errors.append("unknown_recovery.final_status_invalid")
    return reviewed_at, operator_reviewed_by, final_status


def _validate_audit(
    audit: Mapping[str, object],
    started_at: datetime | None,
    completed_at: datetime | None,
    errors: list[str],
) -> tuple[int | None, datetime | None, str | None]:
    _reject_unknown_keys(audit, AUDIT_KEYS, "audit", errors)
    audit_logs_reviewed: int | None = None
    for key, minimum in AUDIT_MINIMUMS.items():
        count = _require_int(audit, key, "audit", errors)
        if count is None:
            continue
        if count < minimum:
            errors.append(f"audit.{key}_below_minimum_{minimum}")
        if key == "audit_logs_reviewed":
            audit_logs_reviewed = count

    reviewed_by = _require_str(audit, "reviewed_by", "audit", errors)
    if reviewed_by is not None and _contains_blocked_operator_identity_segment(reviewed_by):
        errors.append("audit.reviewed_by_must_be_human")
    if reviewed_by is not None and OPERATOR_HANDLE_RE.fullmatch(reviewed_by) is None:
        errors.append("audit.reviewed_by_must_be_logical_operator_id")
    reviewed_at = _require_timestamp(audit, "reviewed_at", "audit", errors)
    _require_inside_window(reviewed_at, started_at, completed_at, "audit.reviewed_at", errors)
    return audit_logs_reviewed, reviewed_at, reviewed_by


def _validate_provider_reviewer_independence(
    unknown_recovery_reviewed_by: str | None,
    audit_reviewed_by: str | None,
    errors: list[str],
) -> None:
    if (
        unknown_recovery_reviewed_by is None
        or audit_reviewed_by is None
        or not _operator_identities_match(
            unknown_recovery_reviewed_by,
            audit_reviewed_by,
        )
    ):
        return
    errors.append("unknown_recovery.operator_reviewed_by_must_differ_from_audit_reviewed_by")


def _validate_unknown_recovery_review_sequence(
    status_summary: StatusSequenceSummary,
    unknown_recovery_reviewed_at: datetime | None,
    errors: list[str],
) -> None:
    latest_observed_at = status_summary.latest_observed_at
    if (
        unknown_recovery_reviewed_at is None
        or latest_observed_at is None
        or unknown_recovery_reviewed_at > latest_observed_at
    ):
        return
    errors.append(
        "unknown_recovery.operator_reviewed_at_must_be_after_latest_provider_status_observed_at"
    )


def _validate_unknown_recovery_final_status_sequence(
    status_summary: StatusSequenceSummary,
    unknown_recovery_final_status: str | None,
    errors: list[str],
) -> None:
    latest_local_status = status_summary.latest_local_status
    if (
        latest_local_status is None
        or unknown_recovery_final_status is None
        or unknown_recovery_final_status == latest_local_status
    ):
        return
    errors.append("unknown_recovery.final_status_must_match_latest_local_status")


def _validate_provider_review_sequence(
    unknown_recovery_reviewed_at: datetime | None,
    audit_reviewed_at: datetime | None,
    errors: list[str],
) -> None:
    if (
        unknown_recovery_reviewed_at is None
        or audit_reviewed_at is None
        or audit_reviewed_at > unknown_recovery_reviewed_at
    ):
        return
    errors.append("audit.reviewed_at_must_be_after_unknown_recovery_operator_reviewed_at")


def _validate_evidence_artifacts(
    artifacts: Sequence[object],
    drill_id: str | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    artifact_event_anchors: Mapping[str, datetime | None],
    errors: list[str],
) -> int:
    seen_types: set[str] = set()
    seen_uris: set[str] = set()
    seen_sha256: set[str] = set()
    for index, item in enumerate(artifacts):
        path = f"evidence_artifacts[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path}_must_be_object")
            continue
        artifact = cast(Mapping[str, object], item)
        _reject_unknown_keys(artifact, EVIDENCE_ARTIFACT_KEYS, path, errors)

        artifact_type = _require_str(artifact, "type", path, errors)
        if artifact_type is not None:
            if artifact_type not in REQUIRED_ARTIFACT_TYPES:
                errors.append(f"{path}.type_invalid")
            if artifact_type in seen_types:
                errors.append(f"{path}.type_duplicate")
            seen_types.add(artifact_type)

        artifact_drill_id = _require_str(artifact, "drill_id", path, errors)
        if artifact_drill_id is not None and drill_id is not None and artifact_drill_id != drill_id:
            errors.append(f"{path}.drill_id_must_match_evidence_drill_id")

        uri = _require_str(artifact, "uri", path, errors)
        if uri is not None:
            _validate_retained_https_artifact_uri(uri, path, errors)
            if _contains_blocked_artifact_uri_term(uri):
                errors.append(f"{path}.uri_must_not_be_mock_fixture_or_local")
            normalized_uri = _canonical_retained_uri_key(uri)
            if normalized_uri in seen_uris:
                errors.append(f"{path}.uri_duplicate")
            else:
                seen_uris.add(normalized_uri)

        sha256 = _require_str(artifact, "sha256", path, errors)
        if sha256 is not None:
            if SHA256_RE.fullmatch(sha256) is None:
                errors.append(f"{path}.sha256_must_be_64_hex")
            else:
                normalized_sha256 = sha256.lower()
                if normalized_sha256 in seen_sha256:
                    errors.append(f"{path}.sha256_duplicate")
                else:
                    seen_sha256.add(normalized_sha256)

        captured_at = _require_timestamp(artifact, "captured_at", path, errors)
        _require_inside_window(
            captured_at,
            started_at,
            completed_at,
            f"{path}.captured_at",
            errors,
        )
        if artifact_type is not None and captured_at is not None:
            anchor = artifact_event_anchors.get(artifact_type)
            if anchor is not None and captured_at <= anchor:
                errors.append(f"{path}.captured_at_must_be_after_{artifact_type}_event")

    missing = REQUIRED_ARTIFACT_TYPES - seen_types
    for artifact_type in sorted(missing):
        errors.append(f"evidence_artifacts.missing_{artifact_type}")
    return len(artifacts)


def _validate_evidence_artifacts_remote_fetch(
    payload: Mapping[str, object],
    *,
    fetcher: RemoteArtifactFetcher,
    timeout_seconds: int,
    errors: list[str],
) -> None:
    artifacts = payload.get("evidence_artifacts")
    if not isinstance(artifacts, list):
        return

    for index, item in enumerate(artifacts):
        if not isinstance(item, Mapping):
            continue
        artifact = cast(Mapping[str, object], item)
        uri = artifact.get("uri")
        sha256 = artifact.get("sha256")
        if not isinstance(uri, str) or not isinstance(sha256, str):
            continue

        path = f"evidence_artifacts[{index}]"
        if _artifact_uri_is_github_blob_page(uri):
            errors.append(f"{path}.uri_remote_must_reference_raw_artifact_bytes")
            continue

        try:
            body = fetcher(uri, timeout_seconds)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            errors.append(f"{path}.uri_remote_fetch_failed")
            continue

        if len(body) > MAX_REMOTE_ARTIFACT_BYTES:
            errors.append(f"{path}.uri_remote_artifact_too_large")
            continue

        actual_sha256 = hashlib.sha256(body).hexdigest()
        if actual_sha256 != sha256.lower():
            errors.append(f"{path}.uri_remote_sha256_mismatch")


def _artifact_uri_is_github_blob_page(uri: str) -> bool:
    parts = urlsplit(uri)
    host = parts.hostname.rstrip(".").casefold() if parts.hostname else ""
    if host not in {"github.com", "www.github.com"}:
        return False
    path_parts = [unquote(part) for part in parts.path.split("/") if part]
    return len(path_parts) >= 5 and path_parts[2] == "blob"


def _default_remote_artifact_fetcher(uri: str, timeout_seconds: int) -> bytes:
    request = Request(
        uri,
        headers={"User-Agent": "kr-auto-trading-lab-live-readiness-verifier"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return cast(bytes, response.read(MAX_REMOTE_ARTIFACT_BYTES + 1))


def _contains_blocked_artifact_uri_term(uri: str) -> bool:
    lowered = uri.lower()
    return any(term in lowered for term in BLOCKED_ARTIFACT_URI_TERMS)


def _operator_identities_match(left: str, right: str) -> bool:
    left_normalized = _normalize_operator_identity(left)
    right_normalized = _normalize_operator_identity(right)
    return left_normalized != "" and left_normalized == right_normalized


def _normalize_operator_identity(value: str) -> str:
    return " ".join(
        segment for segment in re.split(r"[^a-z0-9]+", value.casefold()) if segment
    )


def _contains_blocked_operator_identity_segment(value: str) -> bool:
    segments = tuple(segment for segment in re.split(r"[^a-z0-9]+", value.casefold()) if segment)
    for blocked_term in OPERATOR_REVIEW_BLOCKED_TERMS:
        blocked_segments = tuple(blocked_term.split("-"))
        if len(blocked_segments) == 1:
            if blocked_segments[0] in segments:
                return True
            continue

        compact_blocked_term = "".join(blocked_segments)
        if compact_blocked_term in segments:
            return True
        for index in range(len(segments) - len(blocked_segments) + 1):
            if segments[index : index + len(blocked_segments)] == blocked_segments:
                return True
    return False


def _validate_retained_https_artifact_uri(
    uri: str,
    path: str,
    errors: list[str],
) -> None:
    if "://" not in uri:
        errors.append(f"{path}.uri_must_be_retained_artifact_uri")
        return

    parts = urlsplit(uri)
    if parts.scheme != "https":
        errors.append(f"{path}.uri_must_be_https_uri")
    if not parts.hostname:
        errors.append(f"{path}.uri_must_have_host")
    else:
        _validate_remote_artifact_hostname(parts.hostname, path, errors)
    _validate_retained_artifact_uri_port(parts, path, errors)
    if parts.username or parts.password:
        errors.append(f"{path}.uri_must_not_include_credentials")
    if not parts.path or parts.path == "/":
        errors.append(f"{path}.uri_must_include_artifact_path")
    _validate_retained_artifact_uri_path_segments(parts, path, errors)
    if parts.query or parts.fragment:
        errors.append(f"{path}.uri_must_not_include_query_or_fragment")


def _validate_retained_artifact_uri_path_segments(
    parts: SplitResult,
    path: str,
    errors: list[str],
) -> None:
    for raw_segment in (segment for segment in parts.path.split("/") if segment):
        if _is_ambiguous_retained_artifact_uri_path_segment(raw_segment):
            errors.append(f"{path}.uri_must_not_include_path_traversal")
            return


def _is_ambiguous_retained_artifact_uri_path_segment(segment: str) -> bool:
    value = segment
    for _ in range(4):
        if value in {".", ".."} or "/" in value or "\\" in value:
            return True
        decoded = unquote(value)
        if decoded == value:
            return False
        value = decoded
    return value in {".", ".."} or "/" in value or "\\" in value


def _validate_retained_artifact_uri_port(
    parts: SplitResult,
    path: str,
    errors: list[str],
) -> None:
    try:
        port = parts.port
    except ValueError:
        errors.append(f"{path}.uri_must_have_valid_port")
        return
    if port == 0:
        errors.append(f"{path}.uri_must_have_valid_port")


def _canonical_retained_uri_key(value: str) -> str:
    raw = value.strip()
    parts = urlsplit(raw)
    if not parts.scheme or not parts.hostname:
        return raw.casefold()

    scheme = parts.scheme.casefold()
    hostname = parts.hostname.strip().casefold().rstrip(".")
    try:
        port = parts.port
    except ValueError:
        port = None
    port_part = ""
    if port is not None and not (scheme == "https" and port == 443):
        port_part = f":{port}"
    path = _canonical_retained_uri_path(parts.path)
    return f"{scheme}://{hostname}{port_part}{path}"


def _canonical_retained_uri_path(path: str) -> str:
    return PERCENT_ENCODED_RE.sub(_canonical_retained_uri_percent_escape, path)


def _canonical_retained_uri_percent_escape(match: re.Match[str]) -> str:
    value = match.group(0)
    character = chr(int(value[1:], 16))
    if character in URI_UNRESERVED_CHARS:
        return character
    return value.upper()


def _validate_remote_artifact_hostname(hostname: str, path: str, errors: list[str]) -> None:
    normalized = hostname.strip().lower().rstrip(".")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        address = None
    if address is not None:
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or not address.is_global
        ):
            errors.append(f"{path}.uri_must_be_remote_retained_uri")
        return
    if not _is_valid_retained_dns_hostname(normalized):
        errors.append(f"{path}.uri_must_be_remote_retained_uri")
        return
    if (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or normalized.endswith(PRIVATE_RETAINED_DNS_SUFFIXES)
        or normalized.endswith(".local")
        or normalized.endswith(".test")
        or normalized.endswith(".invalid")
        or normalized.endswith(".example")
        or normalized == "example.com"
        or "." not in normalized
    ):
        errors.append(f"{path}.uri_must_be_remote_retained_uri")


def _is_valid_retained_dns_hostname(hostname: str) -> bool:
    if len(hostname) > 253:
        return False
    labels = hostname.split(".")
    if len(labels) < 2 or any(label == "" for label in labels):
        return False
    for label in labels:
        if len(label) > 63 or label.startswith("-") or label.endswith("-"):
            return False
        if not all(
            character.isascii() and (character.isalnum() or character == "-")
            for character in label
        ):
            return False
    return True


def _require_mapping(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> Mapping[str, object] | None:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        errors.append(f"{path}.{key}_must_be_object")
        return None
    return cast(Mapping[str, object], value)


def _require_list(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> list[object] | None:
    value = parent.get(key)
    if not isinstance(value, list):
        errors.append(f"{path}.{key}_must_be_array")
        return None
    return cast(list[object], value)


def _require_str(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> str | None:
    value = parent.get(key)
    if not isinstance(value, str) or value.strip() == "":
        errors.append(f"{path}.{key}_must_be_non_empty_string")
        return None
    return value


def _require_bool(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> bool | None:
    value = parent.get(key)
    if not isinstance(value, bool):
        errors.append(f"{path}.{key}_must_be_boolean")
        return None
    return value


def _require_int(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> int | None:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{path}.{key}_must_be_integer")
        return None
    return value


def _require_number(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> float | None:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        errors.append(f"{path}.{key}_must_be_number")
        return None
    return float(value)


def _require_timestamp(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> datetime | None:
    value = _require_str(parent, key, path, errors)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{path}.{key}_must_be_iso_timestamp")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{path}.{key}_must_include_timezone")
        return None
    return parsed


def _require_redacted_identifier(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> None:
    value = _require_str(parent, key, path, errors)
    if value is None:
        return
    redacted = value.startswith("redacted:") or "..." in value
    if not redacted:
        errors.append(f"{path}.{key}_must_be_redacted")
        return
    if len(value) > 120 or any(char.isspace() for char in value):
        errors.append(f"{path}.{key}_must_be_short_single_line_redaction")
        return
    if REDACTED_IDENTIFIER_RE.fullmatch(value) is None:
        errors.append(f"{path}.{key}_must_use_allowed_redaction_format")


def _validate_uuid(value: str | None, path: str, errors: list[str]) -> None:
    if value is None:
        return
    try:
        UUID(value)
    except ValueError:
        errors.append(f"{path}_must_be_uuid")


def _require_inside_window(
    value: datetime | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    path: str,
    errors: list[str],
) -> None:
    if value is None or started_at is None or completed_at is None:
        return
    if value < started_at or value > completed_at:
        errors.append(f"{path}_outside_drill_window")


def _require_not_future(value: datetime | None, path: str, errors: list[str]) -> None:
    if value is None:
        return
    if value.astimezone(UTC) > _current_utc() + timedelta(
        seconds=MAX_FUTURE_EVIDENCE_SKEW_SECONDS
    ):
        errors.append(f"{path}_must_not_be_future")


def _current_utc() -> datetime:
    return datetime.now(UTC)


def _scan_for_sensitive_keys(value: object, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY_RE.search(key):
                errors.append(f"sensitive_key_not_allowed:{child_path}")
            _scan_for_sensitive_keys(child, child_path, errors)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_sensitive_keys(child, f"{path}[{index}]", errors)


def _reject_unknown_keys(
    value: Mapping[str, object],
    allowed: Iterable[str],
    path: str,
    errors: list[str],
) -> None:
    allowed_keys = {str(key) for key in allowed}
    unknown = sorted(str(key) for key in value if str(key) not in allowed_keys)
    if unknown:
        errors.append(f"{path}.unknown_keys={','.join(unknown)}")


def _join_errors(errors: Sequence[str]) -> str:
    shown = list(errors[:8])
    if len(errors) > len(shown):
        shown.append(f"additional_errors={len(errors) - len(shown)}")
    return ";".join(shown)


def _safe_reason(reason: str) -> str:
    single_line = reason.replace("\r", " ").replace("\n", " ")
    return single_line[:600]


if __name__ == "__main__":
    raise SystemExit(main())

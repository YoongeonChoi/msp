from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, unquote, urlsplit
from urllib.request import Request, urlopen

from app.application.services.provider_gap_gate import (
    ProviderGapEvidenceValidationError,
    verify_provider_gap_evidence,
)
from app.tools.verify_provider_lifecycle_evidence import (
    EvidenceValidationError as ProviderLifecycleEvidenceValidationError,
)
from app.tools.verify_provider_lifecycle_evidence import (
    ProviderLifecycleEvidenceSummary,
    RemoteArtifactFetcher,
    verify_provider_lifecycle_evidence,
    verify_provider_lifecycle_remote_artifacts,
)

SENSITIVE_KEY_RE = re.compile(
    r"(authorization|secret|client_secret|access_token|refresh_token|"
    r"api[_-]?key|token|password|account_number|account_no|acct_no|jwt)",
    re.IGNORECASE,
)
SCAN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,120}$")
GIT_HEAD_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
SHORT_GIT_SHA_RE = re.compile(r"^[A-Fa-f0-9]{12}$")
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
PERCENT_ENCODED_RE = re.compile(r"%[0-9A-Fa-f]{2}")
INCIDENT_DRILL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
INCIDENT_CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,80}$")
OPERATOR_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,80}$")
KOREAN_STOCK_SYMBOL_RE = re.compile(r"^\d{6}$")
PROVIDER_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,80}$")
URI_UNRESERVED_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
MAX_INCIDENT_ACK_LATENCY_MS = 300_000
MAX_LIVE_ALERT_DRILL_LATENCY_MS = 2_000
MAX_PROVIDER_GAP_WARNING_PARTIAL_GAPS = 1
REMOTE_EVIDENCE_TIMEOUT_SECONDS = 10
MAX_REMOTE_EVIDENCE_BYTES = 5_000_000
NO_PROVIDER_WARNING_GAP_IDS = "none"
DOCUMENTED_PROVIDER_WARNING_GAP_IDS = frozenset(
    {
        "toss:live-account-state-sync-for-scheduled-cycle:"
        "partial-system-only-accepted-fail-closed"
    }
)
PROVIDER_GAP_WARNING_ID_RE = re.compile(r"^[a-z0-9]+:[a-z0-9-]+:[a-z0-9-]+$")
MAX_EVIDENCE_WINDOW_SECONDS = 24 * 60 * 60
MAX_FUTURE_EVIDENCE_SKEW_SECONDS = 5 * 60
INCIDENT_EVIDENCE_BLOCKED_TERMS = (
    "sample",
    "fixture",
    "mock",
    "example.test",
    "localhost",
    "127.0.0.1",
)
INCIDENT_ACK_OPERATOR_BLOCKED_TERMS = (
    "automation",
    "bot",
    "ci",
    "github-actions",
    "script",
    "service-account",
    "system",
)
SYSTEM_ORDER_SCOPE_ACCEPTANCE_OPERATOR_BLOCKED_TERMS = INCIDENT_ACK_OPERATOR_BLOCKED_TERMS
SYSTEM_ORDER_SCOPE_EVIDENCE_BLOCKED_TERMS = INCIDENT_EVIDENCE_BLOCKED_TERMS + (
    "example.com",
    "file://",
    "/tmp/",
    "\\tmp\\",
)
SECURITY_SCAN_REPORT_BLOCKED_TERMS = SYSTEM_ORDER_SCOPE_EVIDENCE_BLOCKED_TERMS
FEATURE_EVIDENCE_BLOCKED_TERMS = SYSTEM_ORDER_SCOPE_EVIDENCE_BLOCKED_TERMS + (
    "static",
    "unconfigured",
)
FEATURE_PROVIDER_BLOCKED_TERMS = (
    "mock",
    "fixture",
    "sample",
    "static",
    "unconfigured",
    "missing",
    "test",
)
PRIVATE_RETAINED_DNS_SUFFIXES = (
    ".corp",
    ".home",
    ".internal",
    ".intranet",
    ".lan",
    ".private",
)
BUNDLE_KEYS = {
    "schema_version",
    "environment",
    "generated_at",
    "reviewed_at",
    "reviewed_by",
    "external_checks",
    "local_checks",
    "provider_lifecycle_evidence",
    "provider_gap_evidence",
    "feature_evidence",
    "system_order_scope_acceptance",
    "security_scan",
}
CHECK_KEYS = {"captured_at", "final_output", "surface", "channel_evidence"}
INCIDENT_CHANNEL_EVIDENCE_KEYS = {
    "captured_at",
    "channel_name",
    "drill_id",
    "evidence_uri",
    "evidence_sha256",
    "operator_ack",
    "operator_ack_at",
    "operator_ack_by",
}
INCIDENT_FINAL_OUTPUT_METRIC_KEYS = {
    "delivered",
    "max_latency_ms",
    "acknowledged",
    "ack_latency_ms",
    "drill_id",
}
HOSTED_SUPABASE_LIVE_READINESS_FINAL_OUTPUT_METRIC_KEYS = {
    "postgrest",
    "anon_rpc_denied",
    "service_rpc_allowed",
    "anon_table_denied",
    "service_table_allowed",
    "authenticated_table_allowed",
    "realtime",
}
HOSTED_LIVE_ENABLE_FLOW_FINAL_OUTPUT_METRIC_KEYS = {
    "requester_admin",
    "reviewer_admin",
    "request_created",
    "self_review_denied",
    "review_accepted",
    "activation_consumed_once",
    "second_activation_denied",
}
LIVE_RECOVERY_DRILL_FINAL_OUTPUT_METRIC_KEYS = {
    "reconciled_updates",
    "manual_check_events",
    "cancel_confirmed",
    "cancel_unknown",
    "status_calls",
    "cancel_calls",
    "pending_order_blocked",
    "manual_check_preserved",
}
LIVE_ALERT_DRILL_FINAL_OUTPUT_METRIC_KEYS = {
    "delivered",
    "max_latency_ms",
}
LIVE_EXECUTION_SAFETY_DRILL_FINAL_OUTPUT_METRIC_KEYS = {
    "missing_evidence_blocked",
    "pre_broker_manual_check",
    "provider_result_recorded",
    "duplicate_blocked",
    "broker_calls",
}
LIVE_READINESS_SCORECARD_FINAL_OUTPUT_METRIC_KEYS = {
    "scorecard_security_scan",
    "worklist_rows",
    "candidate_findings",
    "reportable_findings",
}
WORKER_RELEASE_FRESHNESS_FINAL_OUTPUT_METRIC_KEYS = {
    "expected_sha_short",
    "observed_sha_short",
    "heartbeat_age_sec",
    "max_age_sec",
}
PROVIDER_GAP_FINAL_OUTPUT_METRIC_KEYS = {
    "total_gaps",
    "blocking_unknown_gaps",
    "invalid_status_gaps",
    "warning_partial_gaps",
    "warning_partial_gap_ids",
    "system_order_scope_accepted",
    "provider_gap_evidence",
}
PROVIDER_LIFECYCLE_FINAL_OUTPUT_METRIC_KEYS = {
    "provider",
    "environment",
    "status_observations",
    "audit_logs_reviewed",
    "evidence_artifacts",
}
SYSTEM_ORDER_SCOPE_KEYS = {
    "accepted",
    "scope",
    "broker",
    "limitation",
    "runtime_env_var",
    "runtime_env_value_confirmed",
    "deployment_environment",
    "accepted_by",
    "accepted_at",
    "evidence_captured_at",
    "evidence_uri",
    "evidence_sha256",
}
FEATURE_EVIDENCE_KEYS = {
    "schema_version",
    "captured_at",
    "feature_source",
    "feature_evidence_version",
    "live_trading_ready",
    "symbols",
    "feature_snapshot_count",
    "provider_inputs",
    "feature_artifacts",
}
FEATURE_PROVIDER_INPUT_KEYS = {
    "quote_provider",
    "fundamentals_provider",
    "news_provider",
    "market_sector_provider",
}
FEATURE_ARTIFACT_KEYS = {"type", "symbol", "uri", "sha256", "captured_at"}
FEATURE_ARTIFACT_TYPES = {
    "feature_snapshot_export",
    "quote_evidence",
    "fundamentals_evidence",
    "news_evidence",
    "market_sector_evidence",
}
BUNDLE_ENVIRONMENT_TO_SCOPE_DEPLOYMENT_ENVIRONMENT = {
    "staging": "staging",
    "production-readiness": "production",
}
BUNDLE_ENVIRONMENT_TO_PROVIDER_LIFECYCLE_ENVIRONMENT = {
    "staging": "sandbox",
    "production-readiness": "live",
}
SECURITY_SCAN_KEYS = {
    "scan_id",
    "report_path",
    "report_uri",
    "report_sha256",
    "source_head",
    "source_diff_sha256",
    "completed_at",
    "scan_profile",
    "independent_replay",
    "threat_model_receipt",
    "finding_discovery_receipt",
    "worklist_rows",
    "completion_receipts",
    "candidate_findings",
    "validation_receipts",
    "attack_path_receipts",
    "reportable_findings",
}

EXTERNAL_CHECK_PREFIXES = {
    "hosted_supabase_live_readiness": "FINAL=PASS hosted_supabase_live_readiness",
    "hosted_live_enable_flow": "FINAL=PASS hosted_live_enable_flow",
    "provider_lifecycle_evidence": "FINAL=PASS provider_lifecycle_evidence",
    "live_incident_response_drill": "FINAL=PASS live_incident_response_drill",
}
EXTERNAL_REQUIRED_SURFACES = {
    "hosted_supabase_live_readiness": "hosted_supabase",
    "hosted_live_enable_flow": "hosted_supabase",
    "provider_lifecycle_evidence": "toss_sandbox_or_live",
    "live_incident_response_drill": "real_incident_channel",
}
LOCAL_CHECK_PREFIXES = {
    "worker_release_freshness": "FINAL=PASS worker_release_freshness",
    "live_enable_migration": "FINAL=PASS live_enable_consumed_once rpc_hardening",
    "live_execution_safety_drill": "FINAL=PASS live_execution_safety_drill",
    "live_recovery_drill": "FINAL=PASS live_recovery_drill",
    "live_alert_drill": "FINAL=PASS live_external_alert_drill",
    "provider_contract_gaps": "FINAL=PASS provider_contract_gaps",
    "live_readiness_scorecard": "FINAL=PASS live_readiness_scorecard",
}


class BundleValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LiveReadinessEvidenceBundleSummary:
    external_checks: int
    local_checks: int
    security_scan: bool
    system_order_scope_accepted: bool
    provider_gap_evidence: bool
    feature_evidence: bool
    remote_provider_artifacts: bool = False
    remote_incident_evidence: bool = False
    remote_system_order_scope_evidence: bool = False
    remote_feature_artifacts: bool = False


@dataclass(frozen=True, slots=True)
class IncidentResponseEvidenceSummary:
    delivered: int
    max_latency_ms: int
    ack_latency_ms: int
    channel_name: str
    operator_ack_by: str


@dataclass(frozen=True, slots=True)
class SystemOrderScopeEvidenceSummary:
    scope: str
    broker: str
    deployment_environment: str
    accepted_by: str


@dataclass(frozen=True, slots=True)
class SecurityScanEvidenceSummary:
    scan_id: str
    worklist_rows: int
    completion_receipts: int
    candidate_findings: int
    validation_receipts: int
    attack_path_receipts: int
    report_uri: str


def verify_live_readiness_evidence_bundle_file(
    path: Path,
    *,
    repo_root: Path | None = None,
    verify_remote_provider_artifacts: bool = False,
    remote_provider_artifact_fetcher: RemoteArtifactFetcher | None = None,
    remote_provider_artifact_timeout_seconds: int = 10,
    verify_remote_incident_evidence: bool = False,
    remote_incident_evidence_fetcher: RemoteArtifactFetcher | None = None,
    remote_incident_evidence_timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
    verify_remote_system_order_scope_evidence: bool = False,
    remote_system_order_scope_evidence_fetcher: RemoteArtifactFetcher | None = None,
    remote_system_order_scope_evidence_timeout_seconds: int = (
        REMOTE_EVIDENCE_TIMEOUT_SECONDS
    ),
    verify_remote_feature_artifacts: bool = False,
    remote_feature_artifact_fetcher: RemoteArtifactFetcher | None = None,
    remote_feature_artifact_timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
) -> LiveReadinessEvidenceBundleSummary:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BundleValidationError("bundle_file_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise BundleValidationError("bundle_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise BundleValidationError("bundle_root_must_be_object")
    bundle = cast(Mapping[str, object], payload)
    summary = verify_live_readiness_evidence_bundle(bundle)
    security_scan = bundle.get("security_scan")
    if isinstance(security_scan, Mapping):
        errors: list[str] = []
        _validate_security_scan_report_file_hash(
            cast(Mapping[str, object], security_scan),
            base_dir=path.parent,
            path="security_scan",
            errors=errors,
        )
        if repo_root is not None:
            _validate_security_scan_source_binding(
                cast(Mapping[str, object], security_scan),
                repo_root=repo_root,
                excluded_paths=_security_source_binding_exclusions(
                    path,
                    cast(Mapping[str, object], security_scan),
                ),
                path="security_scan",
                errors=errors,
            )
        if errors:
            raise BundleValidationError(_join_errors(errors))
    provider_lifecycle_evidence = bundle.get("provider_lifecycle_evidence")
    if verify_remote_provider_artifacts and isinstance(provider_lifecycle_evidence, Mapping):
        try:
            verify_provider_lifecycle_remote_artifacts(
                cast(Mapping[str, object], provider_lifecycle_evidence),
                fetcher=remote_provider_artifact_fetcher,
                timeout_seconds=remote_provider_artifact_timeout_seconds,
            )
        except ProviderLifecycleEvidenceValidationError as exc:
            errors = [
                f"provider_lifecycle_evidence.{provider_error}"
                for provider_error in _safe_reason(str(exc)).split(";")
            ]
            raise BundleValidationError(_join_errors(errors)) from exc
    external_checks = bundle.get("external_checks")
    if verify_remote_incident_evidence and isinstance(external_checks, Mapping):
        incident_check = cast(Mapping[str, object], external_checks).get(
            "live_incident_response_drill"
        )
        if isinstance(incident_check, Mapping):
            channel_evidence = cast(Mapping[str, object], incident_check).get(
                "channel_evidence"
            )
            if isinstance(channel_evidence, Mapping):
                verify_incident_response_channel_remote_evidence(
                    cast(Mapping[str, object], channel_evidence),
                    fetcher=remote_incident_evidence_fetcher,
                    timeout_seconds=remote_incident_evidence_timeout_seconds,
                    path="external_checks.live_incident_response_drill.channel_evidence",
                )
    scope_acceptance = bundle.get("system_order_scope_acceptance")
    if verify_remote_system_order_scope_evidence and isinstance(
        scope_acceptance,
        Mapping,
    ):
        verify_system_order_scope_remote_evidence(
            cast(Mapping[str, object], scope_acceptance),
            fetcher=remote_system_order_scope_evidence_fetcher,
            timeout_seconds=remote_system_order_scope_evidence_timeout_seconds,
            path="system_order_scope_acceptance",
        )
    feature_evidence = bundle.get("feature_evidence")
    if verify_remote_feature_artifacts and isinstance(feature_evidence, Mapping):
        verify_feature_evidence_remote_artifacts(
            cast(Mapping[str, object], feature_evidence),
            fetcher=remote_feature_artifact_fetcher,
            timeout_seconds=remote_feature_artifact_timeout_seconds,
            path="feature_evidence",
        )
    return LiveReadinessEvidenceBundleSummary(
        external_checks=summary.external_checks,
        local_checks=summary.local_checks,
        security_scan=summary.security_scan,
        system_order_scope_accepted=summary.system_order_scope_accepted,
        provider_gap_evidence=summary.provider_gap_evidence,
        feature_evidence=summary.feature_evidence,
        remote_provider_artifacts=verify_remote_provider_artifacts,
        remote_incident_evidence=verify_remote_incident_evidence,
        remote_system_order_scope_evidence=verify_remote_system_order_scope_evidence,
        remote_feature_artifacts=verify_remote_feature_artifacts,
    )


def verify_incident_response_evidence_parts(
    *,
    final_output: str,
    channel_evidence: Mapping[str, object],
    generated_at: datetime | None = None,
    reviewed_at: datetime | None = None,
) -> IncidentResponseEvidenceSummary:
    errors: list[str] = []
    check: Mapping[str, object] = {"channel_evidence": channel_evidence}
    _scan_for_sensitive_keys(check, "incident_response_evidence", errors)
    _validate_incident_response_evidence(
        final_output,
        check,
        "incident_response_evidence",
        generated_at,
        reviewed_at,
        errors,
    )
    if errors:
        raise BundleValidationError(_join_errors(errors))

    delivered = _extract_int_metric(final_output, "delivered")
    max_latency_ms = _extract_int_metric(final_output, "max_latency_ms")
    ack_latency_ms = _extract_int_metric(final_output, "ack_latency_ms")
    assert delivered is not None
    assert max_latency_ms is not None
    assert ack_latency_ms is not None
    channel_name = channel_evidence["channel_name"]
    operator_ack_by = channel_evidence["operator_ack_by"]
    assert isinstance(channel_name, str)
    assert isinstance(operator_ack_by, str)
    return IncidentResponseEvidenceSummary(
        delivered=delivered,
        max_latency_ms=max_latency_ms,
        ack_latency_ms=ack_latency_ms,
        channel_name=channel_name,
        operator_ack_by=operator_ack_by,
    )


def verify_system_order_scope_evidence_parts(
    acceptance: Mapping[str, object],
    *,
    generated_at: datetime | None = None,
    reviewed_at: datetime | None = None,
) -> SystemOrderScopeEvidenceSummary:
    errors: list[str] = []
    _scan_for_sensitive_keys(acceptance, "system_order_scope_evidence", errors)
    accepted = _validate_scope_acceptance(
        acceptance,
        generated_at,
        reviewed_at,
        errors,
        path="system_order_scope_evidence",
    )
    if errors:
        raise BundleValidationError(_join_errors(errors))
    assert accepted is True
    scope = acceptance["scope"]
    broker = acceptance["broker"]
    deployment_environment = acceptance["deployment_environment"]
    accepted_by = acceptance["accepted_by"]
    assert isinstance(scope, str)
    assert isinstance(broker, str)
    assert isinstance(deployment_environment, str)
    assert isinstance(accepted_by, str)
    return SystemOrderScopeEvidenceSummary(
        scope=scope,
        broker=broker,
        deployment_environment=deployment_environment,
        accepted_by=accepted_by,
    )


def verify_security_scan_evidence_parts(
    scan: Mapping[str, object],
    *,
    generated_at: datetime | None = None,
    reviewed_at: datetime | None = None,
) -> SecurityScanEvidenceSummary:
    errors: list[str] = []
    _scan_for_sensitive_keys(scan, "security_scan_evidence", errors)
    valid = _validate_security_scan(
        scan,
        generated_at,
        reviewed_at,
        errors,
        path="security_scan_evidence",
    )
    if errors:
        raise BundleValidationError(_join_errors(errors))
    assert valid is True
    scan_id = scan["scan_id"]
    worklist_rows = scan["worklist_rows"]
    completion_receipts = scan["completion_receipts"]
    candidate_findings = scan["candidate_findings"]
    validation_receipts = scan["validation_receipts"]
    attack_path_receipts = scan["attack_path_receipts"]
    report_uri = scan["report_uri"]
    assert isinstance(scan_id, str)
    assert isinstance(worklist_rows, int)
    assert isinstance(completion_receipts, int)
    assert isinstance(candidate_findings, int)
    assert isinstance(validation_receipts, int)
    assert isinstance(attack_path_receipts, int)
    assert isinstance(report_uri, str)
    return SecurityScanEvidenceSummary(
        scan_id=scan_id,
        worklist_rows=worklist_rows,
        completion_receipts=completion_receipts,
        candidate_findings=candidate_findings,
        validation_receipts=validation_receipts,
        attack_path_receipts=attack_path_receipts,
        report_uri=report_uri,
    )


def verify_incident_response_channel_remote_evidence(
    channel_evidence: Mapping[str, object],
    *,
    fetcher: RemoteArtifactFetcher | None = None,
    timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
    path: str = "incident_response_evidence.channel_evidence",
) -> None:
    errors: list[str] = []
    _validate_single_remote_evidence_reference(
        channel_evidence,
        uri_key="evidence_uri",
        sha256_key="evidence_sha256",
        path=path,
        fetcher=fetcher or _default_remote_evidence_fetcher,
        timeout_seconds=timeout_seconds,
        errors=errors,
    )
    if errors:
        raise BundleValidationError(_join_errors(errors))


def verify_system_order_scope_remote_evidence(
    acceptance: Mapping[str, object],
    *,
    fetcher: RemoteArtifactFetcher | None = None,
    timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
    path: str = "system_order_scope_evidence",
) -> None:
    errors: list[str] = []
    _validate_single_remote_evidence_reference(
        acceptance,
        uri_key="evidence_uri",
        sha256_key="evidence_sha256",
        path=path,
        fetcher=fetcher or _default_remote_evidence_fetcher,
        timeout_seconds=timeout_seconds,
        errors=errors,
    )
    if errors:
        raise BundleValidationError(_join_errors(errors))


def verify_feature_evidence_remote_artifacts(
    feature_evidence: Mapping[str, object],
    *,
    fetcher: RemoteArtifactFetcher | None = None,
    timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
    path: str = "feature_evidence",
) -> None:
    errors: list[str] = []
    artifacts = feature_evidence.get("feature_artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
        for index, item in enumerate(artifacts):
            if not isinstance(item, Mapping):
                continue
            _validate_single_remote_evidence_reference(
                cast(Mapping[str, object], item),
                uri_key="uri",
                sha256_key="sha256",
                path=f"{path}.feature_artifacts[{index}]",
                fetcher=fetcher or _default_remote_evidence_fetcher,
                timeout_seconds=timeout_seconds,
                errors=errors,
            )
    if errors:
        raise BundleValidationError(_join_errors(errors))


def verify_live_readiness_evidence_bundle(
    payload: Mapping[str, object],
) -> LiveReadinessEvidenceBundleSummary:
    errors: list[str] = []
    _scan_for_sensitive_keys(payload, "bundle", errors)
    _reject_unknown_keys(payload, BUNDLE_KEYS, "bundle", errors)

    if payload.get("schema_version") != 1:
        errors.append("schema_version_must_be_1")

    environment = _require_str(payload, "environment", "bundle", errors)
    if environment is not None and environment not in {"staging", "production-readiness"}:
        errors.append("environment_must_be_staging_or_production_readiness")

    generated_at = _require_timestamp(payload, "generated_at", "bundle", errors)
    reviewed_at = _require_timestamp(payload, "reviewed_at", "bundle", errors)
    _require_not_future(generated_at, "bundle.generated_at", errors)
    _require_not_future(reviewed_at, "bundle.reviewed_at", errors)
    if generated_at is not None and reviewed_at is not None and reviewed_at <= generated_at:
        errors.append("reviewed_at_must_be_after_generated_at")
    if (
        generated_at is not None
        and reviewed_at is not None
        and (reviewed_at - generated_at).total_seconds() > MAX_EVIDENCE_WINDOW_SECONDS
    ):
        errors.append("evidence_window_must_not_exceed_24h")
    reviewed_by = _require_str(payload, "reviewed_by", "bundle", errors)
    if reviewed_by is not None and _contains_blocked_operator_identity_segment(
        reviewed_by,
        INCIDENT_ACK_OPERATOR_BLOCKED_TERMS,
    ):
        errors.append("bundle.reviewed_by_must_be_human")
    if reviewed_by is not None and OPERATOR_HANDLE_RE.fullmatch(reviewed_by) is None:
        errors.append("bundle.reviewed_by_must_be_logical_operator_id")

    external_checks = _require_mapping(payload, "external_checks", "bundle", errors)
    if external_checks is not None:
        _validate_checks(
            external_checks,
            EXTERNAL_CHECK_PREFIXES,
            required_surfaces=EXTERNAL_REQUIRED_SURFACES,
            path="external_checks",
            generated_at=generated_at,
            reviewed_at=reviewed_at,
            errors=errors,
        )
        _validate_provider_lifecycle_environment_binding(
            bundle_environment=environment,
            external_checks=external_checks,
            errors=errors,
        )
        _validate_incident_ack_reviewer_independence(
            reviewed_by,
            external_checks,
            errors,
        )

    local_checks = _require_mapping(payload, "local_checks", "bundle", errors)
    if local_checks is not None:
        _validate_checks(
            local_checks,
            LOCAL_CHECK_PREFIXES,
            required_surfaces={},
            path="local_checks",
            generated_at=generated_at,
            reviewed_at=reviewed_at,
            errors=errors,
        )

    provider_lifecycle_evidence = _require_mapping(
        payload,
        "provider_lifecycle_evidence",
        "bundle",
        errors,
    )
    if provider_lifecycle_evidence is not None:
        _validate_provider_lifecycle_evidence(
            provider_lifecycle_evidence,
            external_checks,
            bundle_environment=environment,
            errors=errors,
        )
        _validate_provider_lifecycle_reviewer_independence(
            reviewed_by,
            provider_lifecycle_evidence,
            errors,
        )

    provider_gap_evidence = _require_mapping(
        payload,
        "provider_gap_evidence",
        "bundle",
        errors,
    )
    if provider_gap_evidence is not None:
        _validate_provider_gap_evidence(
            provider_gap_evidence,
            local_checks,
            errors,
        )

    feature_evidence = _require_mapping(
        payload,
        "feature_evidence",
        "bundle",
        errors,
    )
    feature_evidence_valid = False
    if feature_evidence is not None:
        feature_evidence_valid = _validate_feature_evidence(
            feature_evidence,
            generated_at,
            reviewed_at,
            errors,
        )

    scope_acceptance = _require_mapping(
        payload,
        "system_order_scope_acceptance",
        "bundle",
        errors,
    )
    scope_accepted = False
    if scope_acceptance is not None:
        scope_accepted = _validate_scope_acceptance(
            scope_acceptance,
            generated_at,
            reviewed_at,
            errors,
        )
        _validate_scope_environment_binding(
            bundle_environment=environment,
            scope_acceptance=scope_acceptance,
            errors=errors,
        )
        _validate_scope_acceptance_reviewer_independence(
            reviewed_by,
            scope_acceptance,
            errors,
        )
        _validate_evidence_operator_role_independence(
            provider_lifecycle_evidence,
            external_checks,
            scope_acceptance,
            errors,
        )

    security_scan = _require_mapping(payload, "security_scan", "bundle", errors)
    security_scan_valid = False
    if security_scan is not None:
        security_scan_valid = _validate_security_scan(
            security_scan,
            generated_at,
            reviewed_at,
            errors,
        )

    _validate_retained_evidence_reference_uniqueness(
        external_checks=external_checks,
        provider_lifecycle_evidence=provider_lifecycle_evidence,
        provider_gap_evidence=provider_gap_evidence,
        feature_evidence=feature_evidence,
        scope_acceptance=scope_acceptance,
        security_scan=security_scan,
        errors=errors,
    )

    if errors:
        raise BundleValidationError(_join_errors(errors))

    assert external_checks is not None
    assert local_checks is not None
    return LiveReadinessEvidenceBundleSummary(
        external_checks=len(EXTERNAL_CHECK_PREFIXES),
        local_checks=len(LOCAL_CHECK_PREFIXES),
        security_scan=security_scan_valid,
        system_order_scope_accepted=scope_accepted,
        provider_gap_evidence=provider_gap_evidence is not None,
        feature_evidence=feature_evidence_valid,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a release-blocking live readiness evidence bundle."
    )
    parser.add_argument(
        "--evidence",
        required=True,
        type=Path,
        help="Path to the live readiness evidence bundle JSON file.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_default_repo_root(),
        help=(
            "Git repository root used to recheck security_scan source_head and source_diff_sha256."
        ),
    )
    parser.add_argument(
        "--verify-remote-provider-artifacts",
        action="store_true",
        help=(
            "Fetch provider_lifecycle_evidence.evidence_artifacts[].uri over HTTPS "
            "and require downloaded bytes to match each declared SHA-256. Use this "
            "after publishing release provider evidence artifacts."
        ),
    )
    parser.add_argument(
        "--verify-remote-incident-evidence",
        action="store_true",
        help=(
            "Fetch external_checks.live_incident_response_drill.channel_evidence."
            "evidence_uri over HTTPS and require downloaded bytes to match "
            "evidence_sha256. Use this after publishing incident-channel evidence."
        ),
    )
    parser.add_argument(
        "--verify-remote-system-order-scope-evidence",
        action="store_true",
        help=(
            "Fetch system_order_scope_acceptance.evidence_uri over HTTPS and require "
            "downloaded bytes to match evidence_sha256. Use this after publishing "
            "system-order scope acceptance evidence."
        ),
    )
    parser.add_argument(
        "--verify-remote-feature-artifacts",
        action="store_true",
        help=(
            "Fetch feature_evidence.feature_artifacts[].uri over HTTPS and require "
            "downloaded bytes to match each declared SHA-256. Use this after "
            "publishing retained provider-live feature artifacts."
        ),
    )
    args = parser.parse_args(argv)

    try:
        summary = verify_live_readiness_evidence_bundle_file(
            args.evidence,
            repo_root=args.repo_root,
            verify_remote_provider_artifacts=args.verify_remote_provider_artifacts,
            verify_remote_incident_evidence=args.verify_remote_incident_evidence,
            verify_remote_system_order_scope_evidence=(
                args.verify_remote_system_order_scope_evidence
            ),
            verify_remote_feature_artifacts=args.verify_remote_feature_artifacts,
        )
    except BundleValidationError as exc:
        print(f"FINAL=FAIL live_readiness_evidence_bundle reason={_safe_reason(str(exc))}")
        return 1

    security_scan = 1 if summary.security_scan else 0
    scope_accepted = 1 if summary.system_order_scope_accepted else 0
    provider_gap_evidence = 1 if summary.provider_gap_evidence else 0
    feature_evidence = 1 if summary.feature_evidence else 0
    remote_provider_artifacts = 1 if summary.remote_provider_artifacts else 0
    remote_incident_evidence = 1 if summary.remote_incident_evidence else 0
    remote_system_order_scope_evidence = (
        1 if summary.remote_system_order_scope_evidence else 0
    )
    remote_feature_artifacts = 1 if summary.remote_feature_artifacts else 0
    print(
        "FINAL=PASS live_readiness_evidence_bundle "
        f"external_checks={summary.external_checks} "
        f"local_checks={summary.local_checks} "
        f"security_scan={security_scan} "
        f"system_order_scope_accepted={scope_accepted} "
        f"provider_gap_evidence={provider_gap_evidence} "
        f"feature_evidence={feature_evidence} "
        f"remote_provider_artifacts={remote_provider_artifacts} "
        f"remote_incident_evidence={remote_incident_evidence} "
        f"remote_system_order_scope_evidence={remote_system_order_scope_evidence} "
        f"remote_feature_artifacts={remote_feature_artifacts}"
    )
    return 0


def _validate_checks(
    checks: Mapping[str, object],
    prefixes: Mapping[str, str],
    *,
    required_surfaces: Mapping[str, str],
    path: str,
    generated_at: datetime | None,
    reviewed_at: datetime | None,
    errors: list[str],
) -> None:
    _reject_unknown_keys(checks, prefixes.keys(), path, errors)
    for name, prefix in prefixes.items():
        check = _require_mapping(checks, name, path, errors)
        if check is None:
            continue
        allowed_check_keys = set(CHECK_KEYS)
        if name != "live_incident_response_drill":
            allowed_check_keys.discard("channel_evidence")
        if name not in required_surfaces:
            allowed_check_keys.discard("surface")
        _reject_unknown_keys(check, allowed_check_keys, f"{path}.{name}", errors)
        final_output = _require_final_output(check, f"{path}.{name}", errors)
        if final_output is not None:
            if not _final_output_has_required_prefix(final_output, prefix):
                errors.append(f"{path}.{name}.final_output_missing_required_pass")
            if "FINAL=SKIP" in final_output or "FINAL=FAIL" in final_output:
                errors.append(f"{path}.{name}.final_output_must_not_be_skip_or_fail")
            if "sample" in final_output.lower() or "fixture" in final_output.lower():
                errors.append(f"{path}.{name}.final_output_must_not_be_sample_or_fixture")

        captured_at = _require_timestamp(check, "captured_at", f"{path}.{name}", errors)
        if captured_at is None:
            continue
        _require_inside_window(
            captured_at,
            generated_at,
            reviewed_at,
            f"{path}.{name}.captured_at",
            errors,
        )

        required_surface = required_surfaces.get(name)
        if required_surface is not None:
            surface = _require_str(check, "surface", f"{path}.{name}", errors)
            if surface is not None and surface != required_surface:
                errors.append(f"{path}.{name}.surface_must_be_{required_surface}")

        if name == "live_incident_response_drill" and final_output is not None:
            _validate_incident_response_evidence(
                final_output,
                check,
                f"{path}.{name}",
                generated_at,
                reviewed_at,
                errors,
            )
        if name == "hosted_supabase_live_readiness" and final_output is not None:
            _validate_hosted_supabase_live_readiness_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "hosted_live_enable_flow" and final_output is not None:
            _validate_hosted_live_enable_flow_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "provider_lifecycle_evidence" and final_output is not None:
            _validate_provider_lifecycle_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "provider_contract_gaps" and final_output is not None:
            _validate_provider_gap_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "live_enable_migration" and final_output is not None:
            _validate_live_enable_migration_final_output(
                final_output,
                prefix,
                f"{path}.{name}",
                errors,
            )
        if name == "live_execution_safety_drill" and final_output is not None:
            _validate_live_execution_safety_drill_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "live_recovery_drill" and final_output is not None:
            _validate_live_recovery_drill_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "live_alert_drill" and final_output is not None:
            _validate_live_alert_drill_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "live_readiness_scorecard" and final_output is not None:
            _validate_live_readiness_scorecard_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )
        if name == "worker_release_freshness" and final_output is not None:
            _validate_worker_release_freshness_final_output_metrics(
                final_output,
                f"{path}.{name}",
                errors,
            )


def _final_output_has_required_prefix(final_output: str, required_prefix: str) -> bool:
    return final_output == required_prefix or final_output.startswith(f"{required_prefix} ")


def _validate_retained_evidence_reference_uniqueness(
    *,
    external_checks: Mapping[str, object] | None,
    provider_lifecycle_evidence: Mapping[str, object] | None,
    provider_gap_evidence: Mapping[str, object] | None,
    feature_evidence: Mapping[str, object] | None,
    scope_acceptance: Mapping[str, object] | None,
    security_scan: Mapping[str, object] | None,
    errors: list[str],
) -> None:
    seen_sha256_paths: dict[str, str] = {}
    seen_uri_paths: dict[str, str] = {}

    def add_sha256(value: object, path: str) -> None:
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            return
        normalized = value.lower()
        previous_path = seen_sha256_paths.get(normalized)
        if previous_path is not None:
            errors.append(f"{path}_duplicates_retained_evidence_sha256")
            return
        seen_sha256_paths[normalized] = path

    def add_uri(value: object, path: str) -> None:
        if not isinstance(value, str) or value.strip() == "":
            return
        normalized = _canonical_retained_uri_key(value)
        previous_path = seen_uri_paths.get(normalized)
        if previous_path is not None:
            errors.append(f"{path}_duplicates_retained_evidence_uri")
            return
        seen_uri_paths[normalized] = path

    if external_checks is not None:
        incident_check = external_checks.get("live_incident_response_drill")
        if isinstance(incident_check, Mapping):
            channel_evidence = incident_check.get("channel_evidence")
            if isinstance(channel_evidence, Mapping):
                add_sha256(
                    channel_evidence.get("evidence_sha256"),
                    "external_checks.live_incident_response_drill.channel_evidence.evidence_sha256",
                )
                add_uri(
                    channel_evidence.get("evidence_uri"),
                    "external_checks.live_incident_response_drill.channel_evidence.evidence_uri",
                )

    if provider_lifecycle_evidence is not None:
        artifacts = provider_lifecycle_evidence.get("evidence_artifacts")
        if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
            for index, item in enumerate(artifacts):
                if not isinstance(item, Mapping):
                    continue
                add_sha256(
                    item.get("sha256"),
                    f"provider_lifecycle_evidence.evidence_artifacts[{index}].sha256",
                )
                add_uri(
                    item.get("uri"),
                    f"provider_lifecycle_evidence.evidence_artifacts[{index}].uri",
                )

    if provider_gap_evidence is not None:
        artifacts = provider_gap_evidence.get("source_artifacts")
        if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
            for index, item in enumerate(artifacts):
                if not isinstance(item, Mapping):
                    continue
                add_sha256(
                    item.get("artifact_sha256"),
                    f"provider_gap_evidence.source_artifacts[{index}].artifact_sha256",
                )
                add_uri(
                    item.get("artifact_uri"),
                    f"provider_gap_evidence.source_artifacts[{index}].artifact_uri",
                )

    if feature_evidence is not None:
        artifacts = feature_evidence.get("feature_artifacts")
        if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
            for index, item in enumerate(artifacts):
                if not isinstance(item, Mapping):
                    continue
                add_sha256(
                    item.get("sha256"),
                    f"feature_evidence.feature_artifacts[{index}].sha256",
                )
                add_uri(
                    item.get("uri"),
                    f"feature_evidence.feature_artifacts[{index}].uri",
                )

    if scope_acceptance is not None:
        add_sha256(
            scope_acceptance.get("evidence_sha256"),
            "system_order_scope_acceptance.evidence_sha256",
        )
        add_uri(
            scope_acceptance.get("evidence_uri"),
            "system_order_scope_acceptance.evidence_uri",
        )

    if security_scan is not None:
        add_sha256(security_scan.get("report_sha256"), "security_scan.report_sha256")
        add_uri(security_scan.get("report_uri"), "security_scan.report_uri")


def _validate_single_remote_evidence_reference(
    evidence: Mapping[str, object],
    *,
    uri_key: str,
    sha256_key: str,
    path: str,
    fetcher: RemoteArtifactFetcher,
    timeout_seconds: int,
    errors: list[str],
) -> None:
    uri = evidence.get(uri_key)
    sha256 = evidence.get(sha256_key)
    if not isinstance(uri, str) or not isinstance(sha256, str):
        return

    if _retained_uri_is_github_blob_page(uri):
        errors.append(f"{path}.{uri_key}_remote_must_reference_raw_artifact_bytes")
        return

    try:
        body = fetcher(uri, timeout_seconds)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        errors.append(f"{path}.{uri_key}_remote_fetch_failed")
        return

    if len(body) > MAX_REMOTE_EVIDENCE_BYTES:
        errors.append(f"{path}.{uri_key}_remote_artifact_too_large")
        return

    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_sha256 != sha256.lower():
        errors.append(f"{path}.{uri_key}_remote_sha256_mismatch")


def _retained_uri_is_github_blob_page(uri: str) -> bool:
    parts = urlsplit(uri)
    host = parts.hostname.rstrip(".").casefold() if parts.hostname else ""
    if host not in {"github.com", "www.github.com"}:
        return False
    path_parts = [unquote(part) for part in parts.path.split("/") if part]
    return len(path_parts) >= 5 and path_parts[2] == "blob"


def _default_remote_evidence_fetcher(uri: str, timeout_seconds: int) -> bytes:
    request = Request(
        uri,
        headers={"User-Agent": "kr-auto-trading-lab-live-readiness-verifier"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return cast(bytes, response.read(MAX_REMOTE_EVIDENCE_BYTES + 1))


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


def _validate_incident_ack(final_output: str, path: str, errors: list[str]) -> None:
    acknowledged = _extract_str_metric(final_output, "acknowledged")
    if acknowledged != "true":
        errors.append(f"{path}.acknowledged_true_required")
    match = re.search(r"\back_latency_ms=(\d+)\b", final_output)
    if match is None:
        errors.append(f"{path}.ack_latency_ms_required")
        return
    ack_latency_ms = int(match.group(1))
    if ack_latency_ms > MAX_INCIDENT_ACK_LATENCY_MS:
        errors.append(f"{path}.ack_latency_ms_above_300000")


def _validate_incident_response_evidence(
    final_output: str,
    check: Mapping[str, object],
    path: str,
    generated_at: datetime | None,
    reviewed_at: datetime | None,
    errors: list[str],
) -> None:
    _validate_incident_final_output_metrics(final_output, path, errors)
    _validate_incident_ack(final_output, path, errors)
    _validate_incident_response_metrics(final_output, path, errors)
    _validate_incident_channel_evidence(
        final_output,
        check,
        path,
        generated_at,
        reviewed_at,
        errors,
    )


def _validate_incident_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    tokens = final_output.split()
    if len(tokens) < 2:
        errors.append(f"{path}.final_output_must_include_check_name")
        return

    unknown_keys: set[str] = set()
    seen_keys: set[str] = set()
    duplicate_keys: set[str] = set()
    for token in tokens[2:]:
        key, separator, _value = token.partition("=")
        if separator != "=" or key == "":
            errors.append(f"{path}.final_output_metrics_must_be_key_value_tokens")
            continue
        if key not in INCIDENT_FINAL_OUTPUT_METRIC_KEYS:
            unknown_keys.add(key)
            continue
        if key in seen_keys:
            duplicate_keys.add(key)
        seen_keys.add(key)

    if unknown_keys:
        errors.append(f"{path}.final_output_unknown_metrics={','.join(sorted(unknown_keys))}")
    if duplicate_keys:
        errors.append(f"{path}.final_output_duplicate_metrics={','.join(sorted(duplicate_keys))}")


def _validate_hosted_supabase_live_readiness_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    _validate_final_output_metric_keys(
        final_output,
        path,
        HOSTED_SUPABASE_LIVE_READINESS_FINAL_OUTPUT_METRIC_KEYS,
        errors,
    )
    required_values = {
        "postgrest": 1,
        "anon_rpc_denied": 2,
        "service_rpc_allowed": 2,
        "anon_table_denied": 1,
        "service_table_allowed": 1,
        "authenticated_table_allowed": 2,
        "realtime": 1,
    }
    for metric_name, required_value in required_values.items():
        value = _require_int_metric(final_output, metric_name, path, errors)
        if value is not None and value != required_value:
            errors.append(f"{path}.{metric_name}_must_be_{required_value}")


def _validate_hosted_live_enable_flow_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    _validate_final_output_metric_keys(
        final_output,
        path,
        HOSTED_LIVE_ENABLE_FLOW_FINAL_OUTPUT_METRIC_KEYS,
        errors,
    )
    for metric_name in sorted(HOSTED_LIVE_ENABLE_FLOW_FINAL_OUTPUT_METRIC_KEYS):
        value = _require_int_metric(final_output, metric_name, path, errors)
        if value is not None and value != 1:
            errors.append(f"{path}.{metric_name}_must_be_1")


def _validate_live_enable_migration_final_output(
    final_output: str,
    required_output: str,
    path: str,
    errors: list[str],
) -> None:
    if final_output != required_output:
        errors.append(f"{path}.final_output_must_match_live_enable_migration_pass")


def _validate_live_execution_safety_drill_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    _validate_final_output_metric_keys(
        final_output,
        path,
        LIVE_EXECUTION_SAFETY_DRILL_FINAL_OUTPUT_METRIC_KEYS,
        errors,
    )
    required_values = {
        "missing_evidence_blocked": 1,
        "pre_broker_manual_check": 1,
        "provider_result_recorded": 1,
        "duplicate_blocked": 1,
        "broker_calls": 1,
    }
    for metric_name, required_value in required_values.items():
        value = _require_int_metric(final_output, metric_name, path, errors)
        if value is not None and value != required_value:
            errors.append(f"{path}.{metric_name}_must_be_{required_value}")


def _validate_live_recovery_drill_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    _validate_final_output_metric_keys(
        final_output,
        path,
        LIVE_RECOVERY_DRILL_FINAL_OUTPUT_METRIC_KEYS,
        errors,
    )
    required_values = {
        "reconciled_updates": 1,
        "manual_check_events": 2,
        "cancel_confirmed": 1,
        "cancel_unknown": 1,
        "status_calls": 4,
        "cancel_calls": 2,
        "pending_order_blocked": 1,
        "manual_check_preserved": 1,
    }
    for metric_name, required_value in required_values.items():
        value = _require_int_metric(final_output, metric_name, path, errors)
        if value is not None and value != required_value:
            errors.append(f"{path}.{metric_name}_must_be_{required_value}")


def _validate_live_alert_drill_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    _validate_final_output_metric_keys(
        final_output,
        path,
        LIVE_ALERT_DRILL_FINAL_OUTPUT_METRIC_KEYS,
        errors,
    )
    delivered = _require_int_metric(final_output, "delivered", path, errors)
    if delivered is not None and delivered != 4:
        errors.append(f"{path}.delivered_must_be_4")
    max_latency_ms = _require_int_metric(final_output, "max_latency_ms", path, errors)
    if max_latency_ms is not None and max_latency_ms < 0:
        errors.append(f"{path}.max_latency_ms_must_be_non_negative")
    if max_latency_ms is not None and max_latency_ms > MAX_LIVE_ALERT_DRILL_LATENCY_MS:
        errors.append(f"{path}.max_latency_ms_above_2000")


def _validate_live_readiness_scorecard_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    _validate_final_output_metric_keys(
        final_output,
        path,
        LIVE_READINESS_SCORECARD_FINAL_OUTPUT_METRIC_KEYS,
        errors,
    )
    scorecard_security_scan = _require_int_metric(
        final_output,
        "scorecard_security_scan",
        path,
        errors,
    )
    if scorecard_security_scan is not None and scorecard_security_scan != 1:
        errors.append(f"{path}.scorecard_security_scan_must_be_1")
    reportable_findings = _require_int_metric(
        final_output,
        "reportable_findings",
        path,
        errors,
    )
    if reportable_findings is not None and reportable_findings != 0:
        errors.append(f"{path}.reportable_findings_must_be_0")
    for metric_name in ("worklist_rows", "candidate_findings"):
        value = _require_int_metric(final_output, metric_name, path, errors)
        if value is not None and value <= 0:
            errors.append(f"{path}.{metric_name}_must_be_positive")


def _validate_worker_release_freshness_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    _validate_final_output_metric_keys(
        final_output,
        path,
        WORKER_RELEASE_FRESHNESS_FINAL_OUTPUT_METRIC_KEYS,
        errors,
    )
    expected_sha_short = _extract_str_metric(final_output, "expected_sha_short")
    if expected_sha_short is None:
        errors.append(f"{path}.expected_sha_short_required")
    elif SHORT_GIT_SHA_RE.fullmatch(expected_sha_short) is None:
        errors.append(f"{path}.expected_sha_short_must_be_12_hex")
    observed_sha_short = _extract_str_metric(final_output, "observed_sha_short")
    if observed_sha_short is None:
        errors.append(f"{path}.observed_sha_short_required")
    elif SHORT_GIT_SHA_RE.fullmatch(observed_sha_short) is None:
        errors.append(f"{path}.observed_sha_short_must_be_12_hex")
    elif expected_sha_short is not None and observed_sha_short != expected_sha_short:
        errors.append(f"{path}.observed_sha_short_must_match_expected")
    heartbeat_age_sec = _require_int_metric(
        final_output,
        "heartbeat_age_sec",
        path,
        errors,
    )
    max_age_sec = _require_int_metric(final_output, "max_age_sec", path, errors)
    if max_age_sec is not None and max_age_sec <= 0:
        errors.append(f"{path}.max_age_sec_must_be_positive")
    if (
        heartbeat_age_sec is not None
        and max_age_sec is not None
        and heartbeat_age_sec > max_age_sec
    ):
        errors.append(f"{path}.heartbeat_age_sec_above_max_age_sec")


def _validate_final_output_metric_keys(
    final_output: str,
    path: str,
    allowed_keys: set[str],
    errors: list[str],
) -> None:
    tokens = final_output.split()
    unknown_keys: set[str] = set()
    seen_keys: set[str] = set()
    duplicate_keys: set[str] = set()
    for token in tokens[2:]:
        key, separator, _value = token.partition("=")
        if separator != "=" or key == "":
            errors.append(f"{path}.final_output_metrics_must_be_key_value_tokens")
            continue
        if key not in allowed_keys:
            unknown_keys.add(key)
            continue
        if key in seen_keys:
            duplicate_keys.add(key)
        seen_keys.add(key)

    missing = allowed_keys - seen_keys
    if missing:
        errors.append(f"{path}.final_output_missing_metrics={','.join(sorted(missing))}")
    if unknown_keys:
        errors.append(f"{path}.final_output_unknown_metrics={','.join(sorted(unknown_keys))}")
    if duplicate_keys:
        errors.append(f"{path}.final_output_duplicate_metrics={','.join(sorted(duplicate_keys))}")


def _validate_provider_lifecycle_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    tokens = final_output.split()
    unknown_keys: set[str] = set()
    seen_keys: set[str] = set()
    duplicate_keys: set[str] = set()
    for token in tokens[2:]:
        key, separator, _value = token.partition("=")
        if separator != "=" or key == "":
            errors.append(f"{path}.final_output_metrics_must_be_key_value_tokens")
            continue
        if key not in PROVIDER_LIFECYCLE_FINAL_OUTPUT_METRIC_KEYS:
            unknown_keys.add(key)
            continue
        if key in seen_keys:
            duplicate_keys.add(key)
        seen_keys.add(key)

    missing = PROVIDER_LIFECYCLE_FINAL_OUTPUT_METRIC_KEYS - seen_keys
    if missing:
        errors.append(f"{path}.final_output_missing_metrics={','.join(sorted(missing))}")
    if unknown_keys:
        errors.append(f"{path}.final_output_unknown_metrics={','.join(sorted(unknown_keys))}")
    if duplicate_keys:
        errors.append(f"{path}.final_output_duplicate_metrics={','.join(sorted(duplicate_keys))}")

    provider = _extract_str_metric(final_output, "provider")
    if provider != "toss":
        errors.append(f"{path}.provider_must_be_toss")
    environment = _extract_str_metric(final_output, "environment")
    if environment not in {"sandbox", "live"}:
        errors.append(f"{path}.environment_must_be_sandbox_or_live")

    status_observations = _require_int_metric(
        final_output,
        "status_observations",
        path,
        errors,
    )
    audit_logs_reviewed = _require_int_metric(
        final_output,
        "audit_logs_reviewed",
        path,
        errors,
    )
    evidence_artifacts = _require_int_metric(
        final_output,
        "evidence_artifacts",
        path,
        errors,
    )
    if status_observations is not None and status_observations < 2:
        errors.append(f"{path}.status_observations_below_2")
    if audit_logs_reviewed is not None and audit_logs_reviewed < 2:
        errors.append(f"{path}.audit_logs_reviewed_below_2")
    if evidence_artifacts is not None and evidence_artifacts != 5:
        errors.append(f"{path}.evidence_artifacts_must_be_5")


def _validate_provider_lifecycle_evidence(
    evidence: Mapping[str, object],
    external_checks: Mapping[str, object] | None,
    *,
    bundle_environment: str | None,
    errors: list[str],
) -> None:
    try:
        summary = verify_provider_lifecycle_evidence(evidence)
    except ProviderLifecycleEvidenceValidationError as exc:
        for provider_error in _safe_reason(str(exc)).split(";"):
            errors.append(f"provider_lifecycle_evidence.{provider_error}")
        return

    if bundle_environment is not None:
        expected_environment = BUNDLE_ENVIRONMENT_TO_PROVIDER_LIFECYCLE_ENVIRONMENT.get(
            bundle_environment
        )
        if expected_environment is not None and summary.environment != expected_environment:
            errors.append("provider_lifecycle_evidence.environment_must_match_bundle_environment")

    final_output = _provider_lifecycle_final_output(external_checks)
    if final_output is None:
        return
    _validate_provider_lifecycle_summary_matches_final_output(summary, final_output, errors)


def _provider_lifecycle_final_output(
    external_checks: Mapping[str, object] | None,
) -> str | None:
    if external_checks is None:
        return None
    check = external_checks.get("provider_lifecycle_evidence")
    if not isinstance(check, Mapping):
        return None
    final_output = check.get("final_output")
    if not isinstance(final_output, str):
        return None
    return final_output


def _validate_provider_lifecycle_summary_matches_final_output(
    summary: ProviderLifecycleEvidenceSummary,
    final_output: str,
    errors: list[str],
) -> None:
    provider = _extract_str_metric(final_output, "provider")
    if provider is not None and provider != summary.provider:
        errors.append("provider_lifecycle_evidence.provider_must_match_final_output")

    environment = _extract_str_metric(final_output, "environment")
    if environment is not None and environment != summary.environment:
        errors.append("provider_lifecycle_evidence.environment_must_match_final_output")

    metric_expectations = {
        "status_observations": summary.status_observations,
        "audit_logs_reviewed": summary.audit_logs_reviewed,
        "evidence_artifacts": summary.evidence_artifacts,
    }
    for metric_name, expected in metric_expectations.items():
        actual = _extract_int_metric(final_output, metric_name)
        if actual is not None and actual != expected:
            errors.append(f"provider_lifecycle_evidence.{metric_name}_must_match_final_output")


def _validate_provider_gap_final_output_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    tokens = final_output.split()
    unknown_keys: set[str] = set()
    seen_keys: set[str] = set()
    duplicate_keys: set[str] = set()
    for token in tokens[2:]:
        key, separator, _value = token.partition("=")
        if separator != "=" or key == "":
            errors.append(f"{path}.final_output_metrics_must_be_key_value_tokens")
            continue
        if key not in PROVIDER_GAP_FINAL_OUTPUT_METRIC_KEYS:
            unknown_keys.add(key)
            continue
        if key in seen_keys:
            duplicate_keys.add(key)
        seen_keys.add(key)

    missing = PROVIDER_GAP_FINAL_OUTPUT_METRIC_KEYS - seen_keys
    if missing:
        errors.append(f"{path}.final_output_missing_metrics={','.join(sorted(missing))}")
    if unknown_keys:
        errors.append(f"{path}.final_output_unknown_metrics={','.join(sorted(unknown_keys))}")
    if duplicate_keys:
        errors.append(f"{path}.final_output_duplicate_metrics={','.join(sorted(duplicate_keys))}")

    total_gaps = _require_int_metric(final_output, "total_gaps", path, errors)
    blocking_unknown_gaps = _require_int_metric(
        final_output,
        "blocking_unknown_gaps",
        path,
        errors,
    )
    invalid_status_gaps = _require_int_metric(
        final_output,
        "invalid_status_gaps",
        path,
        errors,
    )
    warning_partial_gaps = _require_int_metric(
        final_output,
        "warning_partial_gaps",
        path,
        errors,
    )
    if blocking_unknown_gaps is not None and blocking_unknown_gaps != 0:
        errors.append(f"{path}.blocking_unknown_gaps_must_be_0")
    if invalid_status_gaps is not None and invalid_status_gaps != 0:
        errors.append(f"{path}.invalid_status_gaps_must_be_0")
    if total_gaps is not None and total_gaps < 0:
        errors.append(f"{path}.total_gaps_must_be_non_negative")
    if warning_partial_gaps is not None and warning_partial_gaps < 0:
        errors.append(f"{path}.warning_partial_gaps_must_be_non_negative")
    if (
        warning_partial_gaps is not None
        and warning_partial_gaps > MAX_PROVIDER_GAP_WARNING_PARTIAL_GAPS
    ):
        errors.append(f"{path}.warning_partial_gaps_above_1")
    warning_partial_gap_ids = _extract_str_metric(final_output, "warning_partial_gap_ids")
    if warning_partial_gaps is not None and warning_partial_gap_ids is not None:
        _validate_provider_gap_warning_ids(
            warning_partial_gaps,
            warning_partial_gap_ids,
            path,
            errors,
        )
    system_order_scope_accepted = _require_int_metric(
        final_output,
        "system_order_scope_accepted",
        path,
        errors,
    )
    if system_order_scope_accepted is not None and system_order_scope_accepted not in (
        0,
        1,
    ):
        errors.append(f"{path}.system_order_scope_accepted_must_be_0_or_1")
    if (
        warning_partial_gaps is not None
        and warning_partial_gaps > 0
        and system_order_scope_accepted != 1
    ):
        errors.append(f"{path}.system_order_scope_accepted_required_for_warnings")
    if (
        total_gaps is not None
        and blocking_unknown_gaps is not None
        and invalid_status_gaps is not None
        and warning_partial_gaps is not None
        and total_gaps < blocking_unknown_gaps + invalid_status_gaps + warning_partial_gaps
    ):
        errors.append(f"{path}.total_gaps_below_blocking_invalid_plus_warning")
    provider_gap_evidence = _require_int_metric(
        final_output,
        "provider_gap_evidence",
        path,
        errors,
    )
    if provider_gap_evidence is not None and provider_gap_evidence != 1:
        errors.append(f"{path}.provider_gap_evidence_must_be_1")


def _validate_provider_gap_warning_ids(
    warning_partial_gaps: int,
    warning_partial_gap_ids: str,
    path: str,
    errors: list[str],
) -> None:
    if warning_partial_gap_ids == NO_PROVIDER_WARNING_GAP_IDS:
        ids: list[str] = []
    else:
        ids = warning_partial_gap_ids.split(",")
    if len(ids) != len(set(ids)):
        errors.append(f"{path}.warning_partial_gap_ids_must_be_unique")
    if any(PROVIDER_GAP_WARNING_ID_RE.fullmatch(gap_id) is None for gap_id in ids):
        errors.append(f"{path}.warning_partial_gap_ids_must_be_slug_identifiers")
    if warning_partial_gaps != len(ids):
        errors.append(f"{path}.warning_partial_gap_ids_count_must_match_warning_count")
    if warning_partial_gaps == 0 and warning_partial_gap_ids != NO_PROVIDER_WARNING_GAP_IDS:
        errors.append(f"{path}.warning_partial_gap_ids_must_be_none_when_no_warnings")
    if warning_partial_gaps == 1 and set(ids) != DOCUMENTED_PROVIDER_WARNING_GAP_IDS:
        errors.append(
            f"{path}.warning_partial_gap_ids_must_match_documented_toss_scope_limitation"
        )


def _validate_provider_gap_evidence(
    evidence: Mapping[str, object],
    local_checks: Mapping[str, object] | None,
    errors: list[str],
) -> None:
    if local_checks is None:
        return
    provider_gap_check = _require_mapping(
        local_checks,
        "provider_contract_gaps",
        "local_checks",
        errors,
    )
    if provider_gap_check is None:
        return
    final_output = _require_str(
        provider_gap_check,
        "final_output",
        "local_checks.provider_contract_gaps",
        errors,
    )
    if final_output is not None:
        provider_gap_evidence = _extract_int_metric(final_output, "provider_gap_evidence")
        if provider_gap_evidence != 1:
            errors.append("provider_gap_evidence.final_output_must_confirm_evidence")
    api_gaps_path = Path(__file__).resolve().parents[4] / "docs" / "API_GAPS.md"
    try:
        api_gaps_markdown = api_gaps_path.read_text(encoding="utf-8")
    except OSError:
        errors.append("provider_gap_evidence.api_gaps_unreadable")
        return
    try:
        verify_provider_gap_evidence(api_gaps_markdown, evidence)
    except ProviderGapEvidenceValidationError as exc:
        errors.append(f"provider_gap_evidence.invalid:{exc}")


def _validate_feature_evidence(
    evidence: Mapping[str, object],
    generated_at: datetime | None,
    reviewed_at: datetime | None,
    errors: list[str],
) -> bool:
    path = "feature_evidence"
    _reject_unknown_keys(evidence, FEATURE_EVIDENCE_KEYS, path, errors)
    schema_version = _require_int(evidence, "schema_version", path, errors)
    if schema_version is not None and schema_version != 1:
        errors.append(f"{path}.schema_version_must_be_1")

    captured_at = _require_timestamp(evidence, "captured_at", path, errors)
    _require_not_future(captured_at, f"{path}.captured_at", errors)
    _require_inside_window(captured_at, generated_at, reviewed_at, f"{path}.captured_at", errors)

    feature_source = _require_str(evidence, "feature_source", path, errors)
    if feature_source is not None and feature_source != "provider_live_v1":
        errors.append(f"{path}.feature_source_must_be_provider_live_v1")
    evidence_version = _require_str(evidence, "feature_evidence_version", path, errors)
    if evidence_version is not None and evidence_version != "provider_live_v1":
        errors.append(f"{path}.feature_evidence_version_must_be_provider_live_v1")

    live_trading_ready = _require_bool(evidence, "live_trading_ready", path, errors)
    if live_trading_ready is not True:
        errors.append(f"{path}.live_trading_ready_must_be_true")

    symbols = _validate_feature_symbols(evidence.get("symbols"), path, errors)
    snapshot_count = _require_int(evidence, "feature_snapshot_count", path, errors)
    if snapshot_count is not None and snapshot_count <= 0:
        errors.append(f"{path}.feature_snapshot_count_must_be_positive")
    if snapshot_count is not None and symbols and snapshot_count != len(symbols):
        errors.append(f"{path}.feature_snapshot_count_must_match_symbols")

    provider_inputs = _require_mapping(evidence, "provider_inputs", path, errors)
    if provider_inputs is not None:
        _validate_feature_provider_inputs(provider_inputs, errors)

    artifact_types_by_symbol = _validate_feature_artifacts(
        evidence.get("feature_artifacts"),
        symbols,
        captured_at,
        generated_at,
        reviewed_at,
        errors,
    )
    for symbol in symbols:
        observed_types = artifact_types_by_symbol.get(symbol, set())
        missing_types = FEATURE_ARTIFACT_TYPES - observed_types
        if missing_types:
            errors.append(
                f"{path}.feature_artifacts_missing_types_for_{symbol}="
                f"{','.join(sorted(missing_types))}"
            )

    return (
        schema_version == 1
        and feature_source == "provider_live_v1"
        and evidence_version == "provider_live_v1"
        and live_trading_ready is True
        and bool(symbols)
        and snapshot_count == len(symbols)
        and all(
            artifact_types_by_symbol.get(symbol, set()) >= FEATURE_ARTIFACT_TYPES
            for symbol in symbols
        )
    )


def _validate_feature_symbols(
    raw_symbols: object,
    path: str,
    errors: list[str],
) -> tuple[str, ...]:
    if not isinstance(raw_symbols, Sequence) or isinstance(raw_symbols, (str, bytes)):
        errors.append(f"{path}.symbols_must_be_array")
        return ()
    if len(raw_symbols) == 0:
        errors.append(f"{path}.symbols_must_not_be_empty")
        return ()

    symbols: list[str] = []
    seen: set[str] = set()
    for index, raw_symbol in enumerate(raw_symbols):
        item_path = f"{path}.symbols[{index}]"
        if not isinstance(raw_symbol, str) or raw_symbol.strip() == "":
            errors.append(f"{item_path}_must_be_non_empty_string")
            continue
        symbol = raw_symbol.strip()
        if KOREAN_STOCK_SYMBOL_RE.fullmatch(symbol) is None:
            errors.append(f"{item_path}_must_be_6_digit_krx_symbol")
            continue
        if symbol in seen:
            errors.append(f"{item_path}_must_be_unique")
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return tuple(symbols)


def _validate_feature_provider_inputs(
    provider_inputs: Mapping[str, object],
    errors: list[str],
) -> None:
    path = "feature_evidence.provider_inputs"
    _reject_unknown_keys(provider_inputs, FEATURE_PROVIDER_INPUT_KEYS, path, errors)
    for key in sorted(FEATURE_PROVIDER_INPUT_KEYS):
        provider = _require_str(provider_inputs, key, path, errors)
        if provider is None:
            continue
        if PROVIDER_IDENTIFIER_RE.fullmatch(provider) is None:
            errors.append(f"{path}.{key}_must_be_logical_provider_identifier")
        if _contains_blocked_feature_provider_term(provider):
            errors.append(f"{path}.{key}_must_not_be_mock_fixture_or_missing")


def _validate_feature_artifacts(
    raw_artifacts: object,
    symbols: Sequence[str],
    feature_captured_at: datetime | None,
    generated_at: datetime | None,
    reviewed_at: datetime | None,
    errors: list[str],
) -> dict[str, set[str]]:
    path = "feature_evidence.feature_artifacts"
    artifacts_by_symbol: dict[str, set[str]] = {symbol: set() for symbol in symbols}
    if not isinstance(raw_artifacts, Sequence) or isinstance(raw_artifacts, (str, bytes)):
        errors.append(f"{path}_must_be_array")
        return artifacts_by_symbol
    if len(raw_artifacts) == 0:
        errors.append(f"{path}_must_not_be_empty")
        return artifacts_by_symbol

    for index, raw_artifact in enumerate(raw_artifacts):
        item_path = f"{path}[{index}]"
        if not isinstance(raw_artifact, Mapping):
            errors.append(f"{item_path}_must_be_object")
            continue
        artifact = cast(Mapping[str, object], raw_artifact)
        _reject_unknown_keys(artifact, FEATURE_ARTIFACT_KEYS, item_path, errors)

        artifact_type = _require_str(artifact, "type", item_path, errors)
        if artifact_type is not None and artifact_type not in FEATURE_ARTIFACT_TYPES:
            errors.append(f"{item_path}.type_invalid")

        symbol = _require_str(artifact, "symbol", item_path, errors)
        if symbol is not None:
            if KOREAN_STOCK_SYMBOL_RE.fullmatch(symbol) is None:
                errors.append(f"{item_path}.symbol_must_be_6_digit_krx_symbol")
            elif symbol not in symbols:
                errors.append(f"{item_path}.symbol_must_be_in_symbols")

        uri = _require_str(artifact, "uri", item_path, errors)
        if uri is not None:
            if _contains_blocked_feature_evidence_term(uri):
                errors.append(f"{item_path}.uri_must_not_be_mock_fixture_or_local")
            _validate_retained_https_uri(uri, item_path, "uri", errors)

        sha256 = _require_str(artifact, "sha256", item_path, errors)
        if sha256 is not None and SHA256_RE.fullmatch(sha256) is None:
            errors.append(f"{item_path}.sha256_must_be_64_hex")

        captured_at = _require_timestamp(artifact, "captured_at", item_path, errors)
        _require_not_future(captured_at, f"{item_path}.captured_at", errors)
        _require_inside_window(
            captured_at,
            generated_at,
            reviewed_at,
            f"{item_path}.captured_at",
            errors,
        )
        if (
            captured_at is not None
            and feature_captured_at is not None
            and captured_at < feature_captured_at
        ):
            errors.append(f"{item_path}.captured_at_must_not_precede_feature_capture")

        if (
            artifact_type in FEATURE_ARTIFACT_TYPES
            and isinstance(symbol, str)
            and symbol in artifacts_by_symbol
        ):
            observed = artifacts_by_symbol[symbol]
            if artifact_type in observed:
                errors.append(f"{item_path}.type_must_be_unique_per_symbol")
            observed.add(artifact_type)
    return artifacts_by_symbol


def _contains_blocked_feature_provider_term(value: str) -> bool:
    normalized = value.casefold()
    return any(term in normalized for term in FEATURE_PROVIDER_BLOCKED_TERMS)


def _contains_blocked_feature_evidence_term(value: str) -> bool:
    normalized = value.casefold()
    return any(term in normalized for term in FEATURE_EVIDENCE_BLOCKED_TERMS)


def _validate_incident_response_metrics(
    final_output: str,
    path: str,
    errors: list[str],
) -> None:
    delivered = _require_int_metric(final_output, "delivered", path, errors)
    if delivered is not None and delivered != 4:
        errors.append(f"{path}.delivered_must_be_4")
    max_latency_ms = _require_int_metric(final_output, "max_latency_ms", path, errors)
    if max_latency_ms is not None and max_latency_ms < 0:
        errors.append(f"{path}.max_latency_ms_must_be_non_negative")
    if max_latency_ms is not None and max_latency_ms > MAX_LIVE_ALERT_DRILL_LATENCY_MS:
        errors.append(f"{path}.max_latency_ms_above_2000")
    ack_latency_ms = _require_int_metric(final_output, "ack_latency_ms", path, errors)
    if ack_latency_ms is not None and ack_latency_ms < 0:
        errors.append(f"{path}.ack_latency_ms_must_be_non_negative")


def _validate_incident_channel_evidence(
    final_output: str,
    check: Mapping[str, object],
    path: str,
    generated_at: datetime | None,
    reviewed_at: datetime | None,
    errors: list[str],
) -> None:
    evidence = _require_mapping(check, "channel_evidence", path, errors)
    if evidence is None:
        return
    _reject_unknown_keys(
        evidence,
        INCIDENT_CHANNEL_EVIDENCE_KEYS,
        f"{path}.channel_evidence",
        errors,
    )

    channel_name = _require_str(evidence, "channel_name", f"{path}.channel_evidence", errors)
    if channel_name is not None and INCIDENT_CHANNEL_NAME_RE.fullmatch(channel_name) is None:
        errors.append(f"{path}.channel_evidence.channel_name_must_be_logical_identifier")
    evidence_drill_id = _require_str(evidence, "drill_id", f"{path}.channel_evidence", errors)
    output_drill_id = _extract_str_metric(final_output, "drill_id")
    if output_drill_id is None:
        errors.append(f"{path}.drill_id_required")
    elif INCIDENT_DRILL_ID_RE.fullmatch(output_drill_id) is None:
        errors.append(f"{path}.drill_id_invalid")
    elif evidence_drill_id is not None and evidence_drill_id != output_drill_id:
        errors.append(f"{path}.channel_evidence.drill_id_must_match_incident_output")
    if evidence_drill_id is not None and INCIDENT_DRILL_ID_RE.fullmatch(evidence_drill_id) is None:
        errors.append(f"{path}.channel_evidence.drill_id_invalid")
    evidence_uri = _require_str(evidence, "evidence_uri", f"{path}.channel_evidence", errors)
    evidence_sha256 = _require_str(
        evidence,
        "evidence_sha256",
        f"{path}.channel_evidence",
        errors,
    )
    if evidence_sha256 is not None and SHA256_RE.fullmatch(evidence_sha256) is None:
        errors.append(f"{path}.channel_evidence.evidence_sha256_must_be_64_hex")

    operator_ack = _require_bool(
        evidence,
        "operator_ack",
        f"{path}.channel_evidence",
        errors,
    )
    if operator_ack is not True:
        errors.append(f"{path}.channel_evidence.operator_ack_must_be_true")
    operator_ack_by = _require_str(
        evidence,
        "operator_ack_by",
        f"{path}.channel_evidence",
        errors,
    )
    if operator_ack_by is not None and _contains_blocked_operator_identity_segment(
        operator_ack_by,
        INCIDENT_ACK_OPERATOR_BLOCKED_TERMS,
    ):
        errors.append(f"{path}.channel_evidence.operator_ack_by_must_be_human")
    if operator_ack_by is not None and OPERATOR_HANDLE_RE.fullmatch(operator_ack_by) is None:
        errors.append(
            f"{path}.channel_evidence.operator_ack_by_must_be_logical_operator_id"
        )

    captured_at = _require_timestamp(
        evidence,
        "captured_at",
        f"{path}.channel_evidence",
        errors,
    )
    _require_not_future(captured_at, f"{path}.channel_evidence.captured_at", errors)
    _require_inside_window(
        captured_at,
        generated_at,
        reviewed_at,
        f"{path}.channel_evidence.captured_at",
        errors,
    )

    operator_ack_at = _require_timestamp(
        evidence,
        "operator_ack_at",
        f"{path}.channel_evidence",
        errors,
    )
    _require_not_future(
        operator_ack_at,
        f"{path}.channel_evidence.operator_ack_at",
        errors,
    )
    _require_inside_window(
        operator_ack_at,
        generated_at,
        reviewed_at,
        f"{path}.channel_evidence.operator_ack_at",
        errors,
    )
    if (
        captured_at is not None
        and operator_ack_at is not None
        and captured_at <= operator_ack_at
    ):
        errors.append(
            f"{path}.channel_evidence.captured_at_must_be_after_operator_ack_at"
        )

    for field_name, value in (
        ("channel_name", channel_name),
        ("evidence_uri", evidence_uri),
    ):
        if value is not None and _contains_blocked_incident_evidence_term(value):
            errors.append(f"{path}.channel_evidence.{field_name}_must_not_be_mock_or_fixture")
    if evidence_uri is not None:
        _validate_retained_https_uri(
            evidence_uri,
            f"{path}.channel_evidence",
            "evidence_uri",
            errors,
        )


def _contains_blocked_incident_evidence_term(value: str) -> bool:
    normalized = value.casefold()
    return any(term in normalized for term in INCIDENT_EVIDENCE_BLOCKED_TERMS)


def _contains_blocked_operator_identity_segment(
    value: str,
    blocked_terms: Sequence[str],
) -> bool:
    segments = tuple(segment for segment in re.split(r"[^a-z0-9]+", value.casefold()) if segment)
    for blocked_term in blocked_terms:
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


def _operator_identities_match(left: str, right: str) -> bool:
    left_normalized = _normalize_operator_identity(left)
    right_normalized = _normalize_operator_identity(right)
    return left_normalized != "" and left_normalized == right_normalized


def _normalize_operator_identity(value: str) -> str:
    return " ".join(
        segment for segment in re.split(r"[^a-z0-9]+", value.casefold()) if segment
    )


def _validate_scope_acceptance_reviewer_independence(
    reviewed_by: str | None,
    scope_acceptance: Mapping[str, object],
    errors: list[str],
) -> None:
    accepted_by = scope_acceptance.get("accepted_by")
    if (
        reviewed_by is None
        or not isinstance(accepted_by, str)
        or not _operator_identities_match(reviewed_by, accepted_by)
    ):
        return
    errors.append("bundle.reviewed_by_must_differ_from_system_order_scope_accepted_by")


def _validate_incident_ack_reviewer_independence(
    reviewed_by: str | None,
    external_checks: Mapping[str, object],
    errors: list[str],
) -> None:
    if reviewed_by is None:
        return
    for operator_ack_by in _incident_ack_operator_identities(external_checks):
        if _operator_identities_match(reviewed_by, operator_ack_by):
            errors.append("bundle.reviewed_by_must_differ_from_incident_ack_operator")
            return


def _validate_provider_lifecycle_reviewer_independence(
    reviewed_by: str | None,
    provider_lifecycle_evidence: Mapping[str, object],
    errors: list[str],
) -> None:
    if reviewed_by is None:
        return
    for operator_identity in _provider_lifecycle_reviewer_identities(
        provider_lifecycle_evidence
    ):
        if _operator_identities_match(reviewed_by, operator_identity):
            errors.append(
                "bundle.reviewed_by_must_differ_from_provider_lifecycle_reviewer"
            )
            return


def _provider_lifecycle_reviewer_identities(
    provider_lifecycle_evidence: Mapping[str, object],
) -> tuple[str, ...]:
    identities: list[str] = []
    unknown_recovery = provider_lifecycle_evidence.get("unknown_recovery")
    if isinstance(unknown_recovery, Mapping):
        operator_reviewed_by = unknown_recovery.get("operator_reviewed_by")
        if isinstance(operator_reviewed_by, str):
            identities.append(operator_reviewed_by)
    audit = provider_lifecycle_evidence.get("audit")
    if isinstance(audit, Mapping):
        reviewed_by = audit.get("reviewed_by")
        if isinstance(reviewed_by, str):
            identities.append(reviewed_by)
    return tuple(identities)


def _incident_ack_operator_identities(
    external_checks: Mapping[str, object],
) -> tuple[str, ...]:
    incident_check = external_checks.get("live_incident_response_drill")
    if not isinstance(incident_check, Mapping):
        return ()
    channel_evidence = incident_check.get("channel_evidence")
    if not isinstance(channel_evidence, Mapping):
        return ()
    return _incident_channel_ack_operator_identities(channel_evidence)


def _incident_channel_ack_operator_identities(
    channel_evidence: Mapping[str, object],
) -> tuple[str, ...]:
    operator_ack_by = channel_evidence.get("operator_ack_by")
    if isinstance(operator_ack_by, str):
        return (operator_ack_by,)
    return ()


def _scope_acceptance_operator_identities(
    scope_acceptance: Mapping[str, object],
) -> tuple[str, ...]:
    accepted_by = scope_acceptance.get("accepted_by")
    if isinstance(accepted_by, str):
        return (accepted_by,)
    return ()


def _operator_identity_groups_are_distinct(
    *groups: Sequence[str],
) -> bool:
    for left_index, left_group in enumerate(groups):
        for left_identity in left_group:
            for right_group in groups[left_index + 1 :]:
                for right_identity in right_group:
                    if _operator_identities_match(left_identity, right_identity):
                        return False
    return True


def _validate_evidence_operator_role_independence(
    provider_lifecycle_evidence: Mapping[str, object] | None,
    external_checks: Mapping[str, object] | None,
    scope_acceptance: Mapping[str, object],
    errors: list[str],
) -> None:
    if provider_lifecycle_evidence is None or external_checks is None:
        return
    if _operator_identity_groups_are_distinct(
        _provider_lifecycle_reviewer_identities(provider_lifecycle_evidence),
        _incident_ack_operator_identities(external_checks),
        _scope_acceptance_operator_identities(scope_acceptance),
    ):
        return
    errors.append("bundle.evidence_operator_roles_must_be_distinct")


def _require_int_metric(
    final_output: str,
    metric_name: str,
    path: str,
    errors: list[str],
) -> int | None:
    value = _extract_int_metric(final_output, metric_name)
    if value is None:
        errors.append(f"{path}.{metric_name}_required_integer")
    return value


def _extract_int_metric(final_output: str, metric_name: str) -> int | None:
    match = re.search(rf"(?:^|\s){re.escape(metric_name)}=(\d+)(?:\s|$)", final_output)
    if match is None:
        return None
    return int(match.group(1))


def _extract_str_metric(final_output: str, metric_name: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(metric_name)}=([^\s]+)(?:\s|$)", final_output)
    if match is None:
        return None
    return match.group(1)


def _validate_scope_acceptance(
    acceptance: Mapping[str, object],
    generated_at: datetime | None,
    reviewed_at: datetime | None,
    errors: list[str],
    *,
    path: str = "system_order_scope_acceptance",
) -> bool:
    _reject_unknown_keys(acceptance, SYSTEM_ORDER_SCOPE_KEYS, path, errors)
    accepted = _require_bool(acceptance, "accepted", path, errors)
    if accepted is not True:
        errors.append(f"{path}.accepted_must_be_true")
    scope = _require_str(acceptance, "scope", path, errors)
    if scope is not None and scope != "system_created_live_orders_only":
        errors.append(f"{path}.scope_invalid")
    broker = _require_str(acceptance, "broker", path, errors)
    if broker is not None and broker != "toss":
        errors.append(f"{path}.broker_must_be_toss")
    limitation = _require_str(
        acceptance,
        "limitation",
        path,
        errors,
    )
    if limitation is not None and limitation != "broker_wide_closed_order_history_unavailable":
        errors.append(f"{path}.limitation_invalid")
    env_var = _require_str(
        acceptance,
        "runtime_env_var",
        path,
        errors,
    )
    if env_var is not None and env_var != "LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED":
        errors.append(f"{path}.runtime_env_var_invalid")
    env_value_confirmed = _require_bool(
        acceptance,
        "runtime_env_value_confirmed",
        path,
        errors,
    )
    if env_value_confirmed is not True:
        errors.append(f"{path}.runtime_env_value_confirmed_must_be_true")
    deployed_environment = _require_str(
        acceptance,
        "deployment_environment",
        path,
        errors,
    )
    if deployed_environment is not None and deployed_environment not in {"staging", "production"}:
        errors.append(f"{path}.deployment_environment_invalid")
    accepted_by = _require_str(acceptance, "accepted_by", path, errors)
    if accepted_by is not None and _contains_blocked_operator_identity_segment(
        accepted_by,
        SYSTEM_ORDER_SCOPE_ACCEPTANCE_OPERATOR_BLOCKED_TERMS,
    ):
        errors.append(f"{path}.accepted_by_must_be_human")
    if accepted_by is not None and OPERATOR_HANDLE_RE.fullmatch(accepted_by) is None:
        errors.append(f"{path}.accepted_by_must_be_logical_operator_id")
    accepted_at = _require_timestamp(
        acceptance,
        "accepted_at",
        path,
        errors,
    )
    _require_not_future(accepted_at, f"{path}.accepted_at", errors)
    _require_inside_window(
        accepted_at,
        generated_at,
        reviewed_at,
        f"{path}.accepted_at",
        errors,
    )
    evidence_captured_at = _require_timestamp(
        acceptance,
        "evidence_captured_at",
        path,
        errors,
    )
    _require_not_future(
        evidence_captured_at,
        f"{path}.evidence_captured_at",
        errors,
    )
    _require_inside_window(
        evidence_captured_at,
        generated_at,
        reviewed_at,
        f"{path}.evidence_captured_at",
        errors,
    )
    if (
        accepted_at is not None
        and evidence_captured_at is not None
        and evidence_captured_at <= accepted_at
    ):
        errors.append(f"{path}.evidence_captured_at_must_be_after_accepted_at")
    evidence_uri = _require_str(
        acceptance,
        "evidence_uri",
        path,
        errors,
    )
    if evidence_uri is not None and _contains_blocked_system_order_scope_evidence_term(
        evidence_uri
    ):
        errors.append(f"{path}.evidence_uri_must_not_be_mock_or_fixture")
    if evidence_uri is not None:
        _validate_retained_https_uri(evidence_uri, path, "evidence_uri", errors)
    evidence_sha256 = _require_str(
        acceptance,
        "evidence_sha256",
        path,
        errors,
    )
    if evidence_sha256 is not None and SHA256_RE.fullmatch(evidence_sha256) is None:
        errors.append(f"{path}.evidence_sha256_must_be_64_hex")
    return accepted is True


def _validate_scope_environment_binding(
    *,
    bundle_environment: str | None,
    scope_acceptance: Mapping[str, object],
    errors: list[str],
) -> None:
    if bundle_environment is None:
        return
    expected_deployment_environment = BUNDLE_ENVIRONMENT_TO_SCOPE_DEPLOYMENT_ENVIRONMENT.get(
        bundle_environment
    )
    if expected_deployment_environment is None:
        return
    deployed_environment = scope_acceptance.get("deployment_environment")
    if (
        isinstance(deployed_environment, str)
        and deployed_environment != expected_deployment_environment
    ):
        errors.append(
            "system_order_scope_acceptance.deployment_environment_must_match_bundle_environment"
        )


def _validate_provider_lifecycle_environment_binding(
    *,
    bundle_environment: str | None,
    external_checks: Mapping[str, object],
    errors: list[str],
) -> None:
    if bundle_environment is None:
        return
    expected_environment = BUNDLE_ENVIRONMENT_TO_PROVIDER_LIFECYCLE_ENVIRONMENT.get(
        bundle_environment
    )
    if expected_environment is None:
        return
    check = external_checks.get("provider_lifecycle_evidence")
    if not isinstance(check, Mapping):
        return
    final_output = check.get("final_output")
    if not isinstance(final_output, str):
        return
    lifecycle_environment = _extract_str_metric(final_output, "environment")
    if (
        lifecycle_environment in {"sandbox", "live"}
        and lifecycle_environment != expected_environment
    ):
        errors.append(
            "external_checks.provider_lifecycle_evidence.environment_must_match_bundle_environment"
        )


def _contains_blocked_system_order_scope_evidence_term(value: str) -> bool:
    normalized = value.casefold()
    return any(term in normalized for term in SYSTEM_ORDER_SCOPE_EVIDENCE_BLOCKED_TERMS)


def _validate_security_scan(
    scan: Mapping[str, object],
    generated_at: datetime | None,
    reviewed_at: datetime | None,
    errors: list[str],
    *,
    path: str = "security_scan",
) -> bool:
    _reject_unknown_keys(scan, SECURITY_SCAN_KEYS, path, errors)
    scan_id = _require_str(scan, "scan_id", path, errors)
    if scan_id is not None and SCAN_ID_RE.fullmatch(scan_id) is None:
        errors.append(f"{path}.scan_id_must_be_logical_identifier")
    report_path = _require_str(scan, "report_path", path, errors)
    if report_path is not None:
        if not _is_markdown_report_path(report_path):
            errors.append(f"{path}.report_path_must_be_markdown_report")
        _validate_security_scan_report_path_shape(report_path, path, errors)
    report_uri = _require_str(scan, "report_uri", path, errors)
    if report_uri is not None:
        if _contains_blocked_security_report_term(report_uri):
            errors.append(f"{path}.report_uri_must_not_be_mock_fixture_or_local")
        _validate_retained_https_uri(report_uri, path, "report_uri", errors)
        if not _is_markdown_report_path(urlsplit(report_uri).path):
            errors.append(f"{path}.report_uri_must_reference_markdown_report")
    report_sha256 = _require_str(scan, "report_sha256", path, errors)
    if report_sha256 is not None and SHA256_RE.fullmatch(report_sha256) is None:
        errors.append(f"{path}.report_sha256_must_be_64_hex")

    source_head = _require_str(scan, "source_head", path, errors)
    if source_head is not None and GIT_HEAD_RE.fullmatch(source_head) is None:
        errors.append(f"{path}.source_head_must_be_40_hex")
    source_diff_sha256 = _require_str(
        scan,
        "source_diff_sha256",
        path,
        errors,
    )
    if source_diff_sha256 is not None and SHA256_RE.fullmatch(source_diff_sha256) is None:
        errors.append(f"{path}.source_diff_sha256_must_be_64_hex")

    completed_at = _require_timestamp(scan, "completed_at", path, errors)
    _require_not_future(completed_at, f"{path}.completed_at", errors)
    _require_inside_window(
        completed_at,
        generated_at,
        reviewed_at,
        f"{path}.completed_at",
        errors,
    )

    scan_profile = _require_str(scan, "scan_profile", path, errors)
    if scan_profile is not None and scan_profile != "security_diff_scan":
        errors.append(f"{path}.scan_profile_must_be_security_diff_scan")

    independent_replay = _require_bool(scan, "independent_replay", path, errors)
    if independent_replay is not True:
        errors.append(f"{path}.independent_replay_must_be_true")

    threat_model_receipt = _require_bool(scan, "threat_model_receipt", path, errors)
    if threat_model_receipt is not True:
        errors.append(f"{path}.threat_model_receipt_must_be_true")

    finding_discovery_receipt = _require_bool(scan, "finding_discovery_receipt", path, errors)
    if finding_discovery_receipt is not True:
        errors.append(f"{path}.finding_discovery_receipt_must_be_true")

    reportable_findings = _require_int(scan, "reportable_findings", path, errors)
    if reportable_findings is not None and reportable_findings != 0:
        errors.append(f"{path}.reportable_findings_must_be_0")

    worklist_rows = _require_int(scan, "worklist_rows", path, errors)
    if worklist_rows is not None and worklist_rows <= 0:
        errors.append(f"{path}.worklist_rows_must_be_positive")

    completion_receipts = _require_int(scan, "completion_receipts", path, errors)
    if (
        worklist_rows is not None
        and completion_receipts is not None
        and completion_receipts != worklist_rows
    ):
        errors.append(f"{path}.completion_receipts_must_equal_worklist_rows")

    candidate_findings = _require_int(scan, "candidate_findings", path, errors)
    if candidate_findings is not None and candidate_findings < 0:
        errors.append(f"{path}.candidate_findings_must_be_non_negative")

    validation_receipts = _require_int(scan, "validation_receipts", path, errors)
    if (
        candidate_findings is not None
        and validation_receipts is not None
        and validation_receipts != candidate_findings
    ):
        errors.append(f"{path}.validation_receipts_must_equal_candidate_findings")

    attack_path_receipts = _require_int(scan, "attack_path_receipts", path, errors)
    if (
        candidate_findings is not None
        and attack_path_receipts is not None
        and attack_path_receipts != candidate_findings
    ):
        errors.append(f"{path}.attack_path_receipts_must_equal_candidate_findings")

    return (
        scan_profile == "security_diff_scan"
        and independent_replay is True
        and threat_model_receipt is True
        and finding_discovery_receipt is True
        and reportable_findings == 0
        and scan_id is not None
        and report_path is not None
        and report_uri is not None
        and report_sha256 is not None
        and source_head is not None
        and source_diff_sha256 is not None
        and worklist_rows is not None
        and worklist_rows > 0
        and completion_receipts is not None
        and completion_receipts == worklist_rows
        and candidate_findings is not None
        and candidate_findings >= 0
        and validation_receipts is not None
        and validation_receipts == candidate_findings
        and attack_path_receipts is not None
        and attack_path_receipts == candidate_findings
    )


def _contains_blocked_security_report_term(value: str) -> bool:
    normalized = value.casefold()
    return any(term in normalized for term in SECURITY_SCAN_REPORT_BLOCKED_TERMS)


def _is_markdown_report_path(value: str) -> bool:
    return Path(value).suffix.casefold() in {".md", ".markdown"}


def _validate_security_scan_report_path_shape(
    report_path_value: str,
    path: str,
    errors: list[str],
) -> None:
    report_path = Path(report_path_value)
    windows_report_path = PureWindowsPath(report_path_value)
    if (
        report_path.is_absolute()
        or report_path.anchor
        or windows_report_path.is_absolute()
        or windows_report_path.anchor
    ):
        errors.append(f"{path}.report_path_must_be_relative_retained_path")
        return
    if ".." in report_path.parts or ".." in windows_report_path.parts:
        errors.append(f"{path}.report_path_must_stay_under_evidence_dir")


def _validate_retained_https_uri(
    uri: str,
    path: str,
    field_name: str,
    errors: list[str],
) -> None:
    if "://" not in uri:
        errors.append(f"{path}.{field_name}_must_be_retained_uri")
        return

    parts = urlsplit(uri)
    if parts.scheme != "https":
        errors.append(f"{path}.{field_name}_must_be_https_uri")
    if not parts.hostname:
        errors.append(f"{path}.{field_name}_must_have_host")
    else:
        _validate_retained_remote_hostname(parts.hostname, path, field_name, errors)
    _validate_retained_uri_port(parts, path, field_name, errors)
    if parts.username or parts.password:
        errors.append(f"{path}.{field_name}_must_not_include_credentials")
    if not parts.path or parts.path == "/":
        errors.append(f"{path}.{field_name}_must_include_artifact_path")
    _validate_retained_uri_path_segments(parts, path, field_name, errors)
    if parts.query or parts.fragment:
        errors.append(f"{path}.{field_name}_must_not_include_query_or_fragment")


def _validate_retained_uri_path_segments(
    parts: SplitResult,
    path: str,
    field_name: str,
    errors: list[str],
) -> None:
    for raw_segment in (segment for segment in parts.path.split("/") if segment):
        if _is_ambiguous_retained_uri_path_segment(raw_segment):
            errors.append(f"{path}.{field_name}_must_not_include_path_traversal")
            return


def _is_ambiguous_retained_uri_path_segment(segment: str) -> bool:
    value = segment
    for _ in range(4):
        if value in {".", ".."} or "/" in value or "\\" in value:
            return True
        decoded = unquote(value)
        if decoded == value:
            return False
        value = decoded
    return value in {".", ".."} or "/" in value or "\\" in value


def _validate_retained_uri_port(
    parts: SplitResult,
    path: str,
    field_name: str,
    errors: list[str],
) -> None:
    try:
        port = parts.port
    except ValueError:
        errors.append(f"{path}.{field_name}_must_have_valid_port")
        return
    if port == 0:
        errors.append(f"{path}.{field_name}_must_have_valid_port")


def _validate_retained_remote_hostname(
    hostname: str,
    path: str,
    field_name: str,
    errors: list[str],
) -> None:
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
            errors.append(f"{path}.{field_name}_must_be_remote_retained_uri")
        return
    if not _is_valid_retained_dns_hostname(normalized):
        errors.append(f"{path}.{field_name}_must_be_remote_retained_uri")
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
        errors.append(f"{path}.{field_name}_must_be_remote_retained_uri")


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


def _validate_security_scan_report_file_hash(
    scan: Mapping[str, object],
    *,
    base_dir: Path,
    path: str,
    errors: list[str],
) -> None:
    report_path_value = scan.get("report_path")
    report_sha256 = scan.get("report_sha256")
    if not isinstance(report_path_value, str) or not isinstance(report_sha256, str):
        return

    report_path = _resolve_security_scan_report_path(
        scan,
        base_dir=base_dir,
        path=path,
        errors=errors,
    )
    if report_path is None:
        return

    try:
        actual_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    except OSError:
        errors.append(f"{path}.report_path_unreadable")
        return

    if actual_sha256 != report_sha256.lower():
        errors.append(f"{path}.report_sha256_mismatch")


def _validate_security_scan_source_binding(
    scan: Mapping[str, object],
    *,
    repo_root: Path,
    excluded_paths: Sequence[Path],
    path: str,
    errors: list[str],
) -> None:
    try:
        actual_binding = _collect_security_source_binding(
            repo_root,
            excluded_paths=excluded_paths,
        )
    except (OSError, SourceBindingError, UnicodeError):
        errors.append(f"{path}.source_binding_unavailable")
        return

    for key in ("source_head", "source_diff_sha256"):
        if scan.get(key) != actual_binding[key]:
            errors.append(f"{path}.{key}_mismatch")


def _security_source_binding_exclusions(
    bundle_path: Path,
    scan: Mapping[str, object],
) -> tuple[Path, ...]:
    exclusions = [bundle_path]
    report_path = _resolve_security_scan_report_path(scan, base_dir=bundle_path.parent)
    if report_path is not None:
        exclusions.append(report_path)
    return tuple(exclusions)


def _resolve_security_scan_report_path(
    scan: Mapping[str, object],
    *,
    base_dir: Path,
    path: str | None = None,
    errors: list[str] | None = None,
) -> Path | None:
    report_path_value = scan.get("report_path")
    if not isinstance(report_path_value, str):
        return None
    report_path = Path(report_path_value)
    windows_report_path = PureWindowsPath(report_path_value)
    if (
        report_path.is_absolute()
        or report_path.anchor
        or windows_report_path.is_absolute()
        or windows_report_path.anchor
    ):
        if path is not None and errors is not None:
            errors.append(f"{path}.report_path_must_be_relative_retained_path")
        return None

    resolved_base = base_dir.resolve(strict=False)
    resolved_report = (resolved_base / report_path).resolve(strict=False)
    try:
        resolved_report.relative_to(resolved_base)
    except ValueError:
        if path is not None and errors is not None:
            errors.append(f"{path}.report_path_must_stay_under_evidence_dir")
        return None
    return resolved_report


class SourceBindingError(RuntimeError):
    pass


def _collect_security_source_binding(
    repo_root: Path,
    *,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, str]:
    repo_root = _resolve_git_repo_root(repo_root)
    try:
        source_head = _run_git(repo_root, "rev-parse", "HEAD").decode("utf-8").strip()
    except UnicodeError as exc:
        raise SourceBindingError from exc
    if GIT_HEAD_RE.fullmatch(source_head) is None:
        raise SourceBindingError

    excluded_relative_paths = _repo_relative_excluded_paths(repo_root, excluded_paths)
    diff_pathspecs = [
        ".",
        *(f":(exclude){path}" for path in excluded_relative_paths),
    ]
    diff = _run_git(repo_root, "diff", "--binary", "HEAD", "--", *diff_pathspecs)
    untracked = _run_git(
        repo_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    digest = hashlib.sha256()
    digest.update(b"live-readiness-security-source-v1\0")
    digest.update(diff)
    excluded = {excluded_path.resolve() for excluded_path in excluded_paths}
    repo = repo_root.resolve()
    for raw_rel_path in sorted(path for path in untracked.split(b"\0") if path):
        try:
            rel_path = raw_rel_path.decode("utf-8")
        except UnicodeError as exc:
            raise SourceBindingError from exc
        full_path = (repo_root / rel_path).resolve()
        try:
            full_path.relative_to(repo)
        except ValueError as exc:
            raise SourceBindingError from exc
        if full_path in excluded or not full_path.is_file():
            continue
        digest.update(b"\0untracked\0")
        digest.update(raw_rel_path)
        digest.update(b"\0")
        try:
            digest.update(full_path.read_bytes())
        except OSError as exc:
            raise SourceBindingError from exc
    return {
        "source_head": source_head,
        "source_diff_sha256": digest.hexdigest(),
    }


def _resolve_git_repo_root(repo_root: Path) -> Path:
    try:
        output = _run_git(repo_root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    except UnicodeError as exc:
        raise SourceBindingError from exc
    if not output:
        raise SourceBindingError
    return Path(output).resolve()


def _repo_relative_excluded_paths(
    repo_root: Path,
    excluded_paths: Sequence[Path],
) -> list[str]:
    repo = repo_root.resolve()
    relative_paths: set[str] = set()
    for excluded_path in excluded_paths:
        try:
            relative_paths.add(excluded_path.resolve().relative_to(repo).as_posix())
        except ValueError:
            continue
    return sorted(relative_paths)


def _run_git(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceBindingError from exc
    if completed.returncode != 0:
        raise SourceBindingError
    return completed.stdout


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _require_final_output(
    parent: Mapping[str, object],
    path: str,
    errors: list[str],
) -> str | None:
    final_output = _require_str(parent, "final_output", path, errors)
    if final_output is None:
        return None
    if "\n" in final_output or "\r" in final_output:
        errors.append(f"{path}.final_output_must_be_single_line")
    return final_output


def _require_mapping(
    parent: Mapping[str, object], key: str, path: str, errors: list[str]
) -> Mapping[str, object] | None:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        errors.append(f"{path}.{key}_must_be_object")
        return None
    return cast(Mapping[str, object], value)


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
        errors.append(f"{path}_outside_bundle_window")


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
    shown = list(errors[:10])
    if len(errors) > len(shown):
        shown.append(f"additional_errors={len(errors) - len(shown)}")
    return ";".join(shown)


def _safe_reason(reason: str) -> str:
    single_line = reason.replace("\r", " ").replace("\n", " ")
    return single_line[:700]


if __name__ == "__main__":
    raise SystemExit(main())

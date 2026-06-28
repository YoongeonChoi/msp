from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

ALLOWED_PROVIDER_GAP_STATUSES = {
    "partial-system-only-accepted-fail-closed",
    "unknown",
    "verified-budget-enforced",
    "verified-limit-implemented",
    "verified-not-implemented",
    "verified-not-implemented-replaced-by-toss",
    "verified-not-live-critical",
    "verified-partial-fundamentals-implemented",
    "verified-readonly-implemented",
    "verified-structured-output-guarded",
}
NO_PROVIDER_WARNING_GAP_IDS = "none"
MAX_PROVIDER_GAP_EVIDENCE_FUTURE_SKEW_SECONDS = 5 * 60
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
SOURCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,120}$")
PROVIDER_GAP_ID_RE = re.compile(r"^[a-z0-9]+:[a-z0-9-]+:[a-z0-9-]+$")
SENSITIVE_KEY_RE = re.compile(
    r"(authorization|secret|client_secret|access_token|refresh_token|"
    r"api[_-]?key|token|password|account_number|account_no|acct_no|jwt)",
    re.IGNORECASE,
)
PROVIDER_GAP_EVIDENCE_KEYS = {
    "schema_version",
    "api_gaps_sha256",
    "gap_ids",
    "source_artifacts",
    "captured_at",
}
PROVIDER_GAP_EVIDENCE_ARTIFACT_KEYS = {
    "provider",
    "source_name",
    "gap_ids",
    "artifact_uri",
    "artifact_sha256",
    "captured_at",
}
PRIVATE_RETAINED_DNS_SUFFIXES = (
    ".corp",
    ".home",
    ".internal",
    ".intranet",
    ".lan",
    ".private",
)


class ProviderGapEvidenceValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderApiGap:
    provider: str
    gap: str
    status: str
    verification_step: str
    line_number: int


@dataclass(frozen=True, slots=True)
class ProviderGapGateReport:
    gaps: tuple[ProviderApiGap, ...]
    blocking_gaps: tuple[ProviderApiGap, ...]
    warning_gaps: tuple[ProviderApiGap, ...]
    unknown_gaps: tuple[ProviderApiGap, ...]
    invalid_status_gaps: tuple[ProviderApiGap, ...]

    @property
    def passed(self) -> bool:
        return not self.blocking_gaps


@dataclass(frozen=True, slots=True)
class ProviderGapEvidenceSummary:
    gap_count: int
    source_artifacts: int


def provider_gap_gate_release_passed(
    report: ProviderGapGateReport,
    *,
    system_order_scope_accepted: bool,
    provider_gap_evidence_verified: bool = False,
) -> bool:
    return (
        report.passed
        and provider_gap_evidence_verified
        and (not report.warning_gaps or system_order_scope_accepted)
    )


def evaluate_provider_api_gaps(markdown: str) -> ProviderGapGateReport:
    gaps = tuple(parse_provider_api_gaps(markdown))
    unknown_gaps = tuple(gap for gap in gaps if _is_unknown_status(gap.status))
    invalid_status_gaps = tuple(
        gap for gap in gaps if not _is_known_status(gap.status)
    )
    blocking_gaps = tuple(
        gap
        for gap in gaps
        if _is_unknown_status(gap.status) or not _is_known_status(gap.status)
    )
    warning_gaps = tuple(gap for gap in gaps if _is_warning_status(gap.status))
    return ProviderGapGateReport(
        gaps=gaps,
        blocking_gaps=blocking_gaps,
        warning_gaps=warning_gaps,
        unknown_gaps=unknown_gaps,
        invalid_status_gaps=invalid_status_gaps,
    )


def parse_provider_api_gaps(markdown: str) -> list[ProviderApiGap]:
    gaps: list[ProviderApiGap] = []
    for line_number, raw_line in enumerate(markdown.splitlines(), start=1):
        line = raw_line.strip()
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or cells[0] == "Provider":
            continue
        gaps.append(
            ProviderApiGap(
                provider=cells[0],
                gap=cells[1],
                status=cells[2],
                verification_step=cells[3],
                line_number=line_number,
            )
        )
    return gaps


def format_provider_gap_gate_report(
    report: ProviderGapGateReport,
    *,
    system_order_scope_accepted: bool = False,
    provider_gap_evidence_verified: bool = False,
) -> str:
    lines = [
        "Provider API Gap Gate",
        f"total_gaps={len(report.gaps)}",
        f"blocking_unknown_gaps={len(report.unknown_gaps)}",
        f"invalid_status_gaps={len(report.invalid_status_gaps)}",
        f"warning_partial_gaps={len(report.warning_gaps)}",
    ]
    if report.blocking_gaps:
        lines.extend(["", "[blocking]"])
        lines.extend(_format_gap(gap) for gap in report.blocking_gaps)
    if report.warning_gaps:
        lines.extend(["", "[warnings]"])
        lines.extend(_format_gap(gap) for gap in report.warning_gaps)
    lines.extend(
        [
            "",
            format_provider_gap_gate_final_line(
                report,
                system_order_scope_accepted=system_order_scope_accepted,
                provider_gap_evidence_verified=provider_gap_evidence_verified,
            ),
        ]
    )
    return "\n".join(lines)


def format_provider_gap_gate_final_line(
    report: ProviderGapGateReport,
    *,
    system_order_scope_accepted: bool = False,
    provider_gap_evidence_verified: bool = False,
) -> str:
    passed = provider_gap_gate_release_passed(
        report,
        system_order_scope_accepted=system_order_scope_accepted,
        provider_gap_evidence_verified=provider_gap_evidence_verified,
    )
    return (
        f"FINAL={'PASS' if passed else 'FAIL'} provider_contract_gaps "
        f"total_gaps={len(report.gaps)} "
        f"blocking_unknown_gaps={len(report.unknown_gaps)} "
        f"invalid_status_gaps={len(report.invalid_status_gaps)} "
        f"warning_partial_gaps={len(report.warning_gaps)} "
        f"warning_partial_gap_ids={_format_warning_gap_ids(report.warning_gaps)} "
        f"system_order_scope_accepted={1 if system_order_scope_accepted else 0} "
        f"provider_gap_evidence={1 if provider_gap_evidence_verified else 0}"
    )


def verify_provider_gap_evidence(
    api_gaps_markdown: str,
    evidence: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> ProviderGapEvidenceSummary:
    errors: list[str] = []
    _scan_for_sensitive_keys(evidence, "provider_gap_evidence", errors)
    _reject_unknown_keys(
        evidence,
        PROVIDER_GAP_EVIDENCE_KEYS,
        "provider_gap_evidence",
        errors,
    )
    if evidence.get("schema_version") != 1:
        errors.append("provider_gap_evidence.schema_version_must_be_1")

    captured_at = _require_timestamp(
        evidence,
        "captured_at",
        "provider_gap_evidence",
        errors,
    )
    _require_not_future(captured_at, "provider_gap_evidence.captured_at", errors, now)

    expected_api_gaps_sha256 = hashlib.sha256(
        api_gaps_markdown.encode("utf-8")
    ).hexdigest()
    api_gaps_sha256 = _require_str(
        evidence,
        "api_gaps_sha256",
        "provider_gap_evidence",
        errors,
    )
    if api_gaps_sha256 is not None and api_gaps_sha256 != expected_api_gaps_sha256:
        errors.append("provider_gap_evidence.api_gaps_sha256_must_match_api_gaps")

    report = evaluate_provider_api_gaps(api_gaps_markdown)
    expected_gap_ids = tuple(provider_api_gap_id(gap) for gap in report.gaps)
    expected_gap_id_set = set(expected_gap_ids)
    expected_gap_provider_by_id = {
        provider_api_gap_id(gap): gap.provider for gap in report.gaps
    }
    gap_ids = _require_str_list(
        evidence,
        "gap_ids",
        "provider_gap_evidence",
        errors,
    )
    if gap_ids is not None:
        if len(gap_ids) != len(set(gap_ids)):
            errors.append("provider_gap_evidence.gap_ids_must_be_unique")
        if any(PROVIDER_GAP_ID_RE.fullmatch(gap_id) is None for gap_id in gap_ids):
            errors.append("provider_gap_evidence.gap_ids_must_be_slug_identifiers")
        if tuple(gap_ids) != expected_gap_ids:
            errors.append("provider_gap_evidence.gap_ids_must_match_api_gaps")

    source_artifacts = _require_mapping_list(
        evidence,
        "source_artifacts",
        "provider_gap_evidence",
        errors,
    )
    covered_gap_ids: set[str] = set()
    seen_artifact_uris: set[str] = set()
    seen_artifact_hashes: set[str] = set()
    source_artifact_count = 0
    if source_artifacts is not None:
        source_artifact_count = len(source_artifacts)
        if not source_artifacts:
            errors.append("provider_gap_evidence.source_artifacts_required")
        for index, artifact in enumerate(source_artifacts):
            path = f"provider_gap_evidence.source_artifacts[{index}]"
            _validate_provider_gap_source_artifact(
                artifact,
                path,
                expected_gap_id_set,
                expected_gap_provider_by_id,
                covered_gap_ids,
                seen_artifact_uris,
                seen_artifact_hashes,
                errors,
                now,
            )

    if covered_gap_ids != expected_gap_id_set:
        errors.append("provider_gap_evidence.source_artifacts_must_cover_every_gap")
    if errors:
        raise ProviderGapEvidenceValidationError(_join_errors(errors))
    return ProviderGapEvidenceSummary(
        gap_count=len(expected_gap_ids),
        source_artifacts=source_artifact_count,
    )


def provider_api_gap_id(gap: ProviderApiGap) -> str:
    return f"{_slug(gap.provider)}:{_slug(gap.gap)}:{_slug(gap.status)}"


def _format_gap(gap: ProviderApiGap) -> str:
    return (
        f"{gap.provider} line={gap.line_number} status={gap.status} "
        f"gap={gap.gap} verify={gap.verification_step}"
    )


def _format_warning_gap_ids(gaps: tuple[ProviderApiGap, ...]) -> str:
    if not gaps:
        return NO_PROVIDER_WARNING_GAP_IDS
    return ",".join(provider_api_gap_id(gap) for gap in gaps)


def _slug(value: str) -> str:
    chars: list[str] = []
    previous_was_separator = False
    for char in value.strip().casefold():
        if char.isalnum():
            chars.append(char)
            previous_was_separator = False
            continue
        if chars and not previous_was_separator:
            chars.append("-")
            previous_was_separator = True
    return "".join(chars).strip("-")


def _is_unknown_status(status: str) -> bool:
    return status.strip().lower() == "unknown"


def _is_known_status(status: str) -> bool:
    return status.strip().lower() in ALLOWED_PROVIDER_GAP_STATUSES


def _is_warning_status(status: str) -> bool:
    normalized = status.strip().lower()
    return (
        normalized.startswith("partial-")
        and normalized in ALLOWED_PROVIDER_GAP_STATUSES
    )


def _validate_provider_gap_source_artifact(
    artifact: Mapping[str, object],
    path: str,
    expected_gap_ids: set[str],
    expected_gap_provider_by_id: Mapping[str, str],
    covered_gap_ids: set[str],
    seen_artifact_uris: set[str],
    seen_artifact_hashes: set[str],
    errors: list[str],
    now: datetime | None,
) -> None:
    _scan_for_sensitive_keys(artifact, path, errors)
    _reject_unknown_keys(
        artifact,
        PROVIDER_GAP_EVIDENCE_ARTIFACT_KEYS,
        path,
        errors,
    )
    provider = _require_str(artifact, "provider", path, errors)
    source_name = _require_str(artifact, "source_name", path, errors)
    if source_name is not None and SOURCE_NAME_RE.fullmatch(source_name) is None:
        errors.append(f"{path}.source_name_must_be_logical_identifier")
    artifact_gap_ids = _require_str_list(artifact, "gap_ids", path, errors)
    if artifact_gap_ids is not None:
        if not artifact_gap_ids:
            errors.append(f"{path}.gap_ids_required")
        if len(artifact_gap_ids) != len(set(artifact_gap_ids)):
            errors.append(f"{path}.gap_ids_must_be_unique")
        for gap_id in artifact_gap_ids:
            if PROVIDER_GAP_ID_RE.fullmatch(gap_id) is None:
                errors.append(f"{path}.gap_ids_must_be_slug_identifiers")
                continue
            if gap_id not in expected_gap_ids:
                errors.append(f"{path}.gap_id_must_exist_in_api_gaps")
                continue
            covered_gap_ids.add(gap_id)
            if provider is not None and provider != expected_gap_provider_by_id[gap_id]:
                errors.append(f"{path}.provider_must_match_gap_provider")

    artifact_uri = _require_str(artifact, "artifact_uri", path, errors)
    if artifact_uri is not None:
        canonical_uri = _validate_retained_https_uri(
            artifact_uri,
            path,
            "artifact_uri",
            errors,
        )
        if canonical_uri is not None:
            if canonical_uri in seen_artifact_uris:
                errors.append(f"{path}.artifact_uri_must_be_unique")
            seen_artifact_uris.add(canonical_uri)

    artifact_sha256 = _require_str(artifact, "artifact_sha256", path, errors)
    if artifact_sha256 is not None:
        normalized_sha256 = artifact_sha256.lower()
        if SHA256_RE.fullmatch(artifact_sha256) is None:
            errors.append(f"{path}.artifact_sha256_must_be_64_hex")
        elif normalized_sha256 in seen_artifact_hashes:
            errors.append(f"{path}.artifact_sha256_must_be_unique")
        seen_artifact_hashes.add(normalized_sha256)

    artifact_captured_at = _require_timestamp(artifact, "captured_at", path, errors)
    _require_not_future(artifact_captured_at, f"{path}.captured_at", errors, now)


def _reject_unknown_keys(
    payload: Mapping[str, object],
    allowed_keys: set[str],
    path: str,
    errors: list[str],
) -> None:
    unknown = set(payload) - allowed_keys
    if unknown:
        errors.append(f"{path}.unknown_keys={','.join(sorted(unknown))}")


def _scan_for_sensitive_keys(value: object, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY_RE.search(key):
                errors.append(f"{child_path}_must_not_be_secret_like")
            _scan_for_sensitive_keys(child, child_path, errors)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_sensitive_keys(child, f"{path}[{index}]", errors)


def _require_str(
    payload: Mapping[str, object],
    key: str,
    path: str,
    errors: list[str],
) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or value.strip() == "":
        errors.append(f"{path}.{key}_required")
        return None
    return value


def _require_str_list(
    payload: Mapping[str, object],
    key: str,
    path: str,
    errors: list[str],
) -> list[str] | None:
    value = payload.get(key)
    if not isinstance(value, list):
        errors.append(f"{path}.{key}_required")
        return None
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or item.strip() == "":
            errors.append(f"{path}.{key}[{index}]_must_be_string")
            continue
        result.append(item)
    return result


def _require_mapping_list(
    payload: Mapping[str, object],
    key: str,
    path: str,
    errors: list[str],
) -> list[Mapping[str, object]] | None:
    value = payload.get(key)
    if not isinstance(value, list):
        errors.append(f"{path}.{key}_required")
        return None
    result: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"{path}.{key}[{index}]_must_be_object")
            continue
        result.append(item)
    return result


def _require_timestamp(
    payload: Mapping[str, object],
    key: str,
    path: str,
    errors: list[str],
) -> datetime | None:
    raw_value = payload.get(key)
    if not isinstance(raw_value, str):
        errors.append(f"{path}.{key}_required")
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{path}.{key}_must_be_iso8601")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{path}.{key}_must_include_timezone")
        return None
    return parsed.astimezone(UTC)


def _require_not_future(
    value: datetime | None,
    path: str,
    errors: list[str],
    now: datetime | None,
) -> None:
    if value is None:
        return
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    if (value - current_time).total_seconds() > MAX_PROVIDER_GAP_EVIDENCE_FUTURE_SKEW_SECONDS:
        errors.append(f"{path}_must_not_be_future")


def _validate_retained_https_uri(
    uri: str,
    path: str,
    field_name: str,
    errors: list[str],
) -> str | None:
    parsed = urlsplit(uri)
    if parsed.scheme != "https":
        errors.append(f"{path}.{field_name}_must_be_https")
        return None
    if parsed.username is not None or parsed.password is not None:
        errors.append(f"{path}.{field_name}_must_not_include_credentials")
    if parsed.query or parsed.fragment:
        errors.append(f"{path}.{field_name}_must_not_include_query_or_fragment")
    try:
        port = parsed.port
    except ValueError:
        errors.append(f"{path}.{field_name}_port_invalid")
        return None
    hostname = parsed.hostname
    if hostname is None or hostname.strip() == "":
        errors.append(f"{path}.{field_name}_host_required")
        return None
    normalized_host = hostname.rstrip(".").casefold()
    if normalized_host in {"localhost", "example.test"}:
        errors.append(f"{path}.{field_name}_host_must_be_public")
    if any(
        normalized_host.endswith(suffix)
        for suffix in PRIVATE_RETAINED_DNS_SUFFIXES
    ):
        errors.append(f"{path}.{field_name}_host_must_not_use_private_suffix")
    try:
        ip_address = ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        if not ip_address.is_global:
            errors.append(f"{path}.{field_name}_host_must_be_global")
    decoded_path = unquote(parsed.path)
    if "\\\\" in decoded_path or "/../" in f"/{decoded_path.lstrip('/')}":
        errors.append(f"{path}.{field_name}_path_must_not_escape")
    normalized_port = "" if port in (None, 443) else f":{port}"
    return f"https://{normalized_host}{normalized_port}{parsed.path}"


def _join_errors(errors: Sequence[str]) -> str:
    return ";".join(errors)

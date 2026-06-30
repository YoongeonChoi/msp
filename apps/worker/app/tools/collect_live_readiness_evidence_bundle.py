from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.tools.verify_live_readiness_evidence_bundle import (
    BundleValidationError,
    LiveReadinessEvidenceBundleSummary,
    _incident_channel_ack_operator_identities,
    _operator_identities_match,
    _operator_identity_groups_are_distinct,
    _provider_lifecycle_reviewer_identities,
    _resolve_security_scan_report_path,
    _scope_acceptance_operator_identities,
    verify_incident_response_evidence_parts,
    verify_live_readiness_evidence_bundle,
    verify_security_scan_evidence_parts,
    verify_system_order_scope_evidence_parts,
)
from app.tools.verify_security_scan_evidence import (
    SecurityScanEvidenceValidationError,
    verify_security_scan_report_file_hash,
)

SENSITIVE_KEY_RE = re.compile(
    r"(authorization|secret|client_secret|access_token|refresh_token|"
    r"api[_-]?key|token|password|account_number|account_no|acct_no|jwt)",
    re.IGNORECASE,
)
ALLOWED_SECURITY_SCAN_KEYS = {
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
ALLOWED_INCIDENT_CHANNEL_EVIDENCE_KEYS = {
    "captured_at",
    "channel_name",
    "drill_id",
    "evidence_uri",
    "evidence_sha256",
    "operator_ack",
    "operator_ack_at",
    "operator_ack_by",
}
ALLOWED_SYSTEM_ORDER_SCOPE_EVIDENCE_KEYS = {
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


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    command: tuple[str, ...]
    cwd: Path
    surface: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    repo_root: Path
    environment: str
    reviewed_by: str
    provider_evidence: Path
    provider_gap_evidence: Path
    incident_output_file: Path
    incident_channel_evidence: Path
    security_scan_summary: Path
    system_order_scope_evidence: Path
    system_order_scope_accepted_by: str
    output: Path


CommandRunner = Callable[[CommandSpec], CommandResult]
Clock = Callable[[], datetime]
GitEvidenceReader = Callable[[Path], dict[str, str]]


def collect_live_readiness_evidence_bundle(
    config: CollectorConfig,
    *,
    command_runner: CommandRunner | None = None,
    git_evidence_reader: GitEvidenceReader | None = None,
    clock: Clock | None = None,
) -> tuple[dict[str, object], LiveReadinessEvidenceBundleSummary]:
    runner = command_runner or _run_command
    now = clock or _now_utc
    collection_started_at = now()
    apps_worker = config.repo_root / "apps" / "worker"
    _validate_collector_artifact_paths(
        config.provider_evidence,
        config.provider_gap_evidence,
        config.incident_output_file,
        config.incident_channel_evidence,
        config.security_scan_summary,
        config.system_order_scope_evidence,
        config.output,
    )
    _validate_collector_output_path(config.output)
    provider_lifecycle_evidence = _load_provider_lifecycle_evidence(config.provider_evidence)
    provider_gap_evidence = _load_provider_gap_evidence(config.provider_gap_evidence)
    if any(
        _operator_identities_match(config.reviewed_by, operator_identity)
        for operator_identity in _provider_lifecycle_reviewer_identities(
            provider_lifecycle_evidence
        )
    ):
        raise CollectorError(
            "bundle.reviewed_by_must_differ_from_provider_lifecycle_reviewer"
        )
    security_scan = _load_security_scan_summary(config.security_scan_summary)
    try:
        verify_security_scan_evidence_parts(security_scan)
    except BundleValidationError as exc:
        raise CollectorError(str(exc)) from exc
    try:
        verify_security_scan_report_file_hash(
            security_scan,
            evidence_path=config.security_scan_summary,
        )
    except SecurityScanEvidenceValidationError as exc:
        raise CollectorError(str(exc)) from exc
    security_report_path = _security_scan_report_path(
        security_scan,
        summary_path=config.security_scan_summary,
    )
    if security_report_path is not None:
        _validate_collector_artifact_paths(
            config.provider_evidence,
            config.provider_gap_evidence,
            config.incident_output_file,
            config.incident_channel_evidence,
            config.security_scan_summary,
            config.system_order_scope_evidence,
            config.output,
            security_report_path,
        )
    excluded_paths = [
        config.provider_evidence,
        config.provider_gap_evidence,
        config.incident_output_file,
        config.incident_channel_evidence,
        config.security_scan_summary,
        config.system_order_scope_evidence,
        config.output,
    ]
    if security_report_path is not None:
        excluded_paths.append(security_report_path)
    if git_evidence_reader is None:
        git_evidence = _collect_git_evidence(
            config.repo_root,
            excluded_paths=excluded_paths,
        )
    else:
        git_evidence = git_evidence_reader(config.repo_root)
    _validate_security_scan_git_binding(security_scan, git_evidence)
    incident_channel_evidence = _load_incident_channel_evidence(config.incident_channel_evidence)
    incident_final_output = _extract_single_final_line(
        config.incident_output_file.read_text(encoding="utf-8"),
        "live_incident_response_drill",
    )
    try:
        verify_incident_response_evidence_parts(
            final_output=incident_final_output,
            channel_evidence=incident_channel_evidence,
        )
    except BundleValidationError as exc:
        raise CollectorError(str(exc)) from exc
    operator_ack_by = incident_channel_evidence.get("operator_ack_by")
    if isinstance(operator_ack_by, str) and _operator_identities_match(
        config.reviewed_by,
        operator_ack_by,
    ):
        raise CollectorError("bundle.reviewed_by_must_differ_from_incident_ack_operator")
    system_order_scope_acceptance = _load_system_order_scope_evidence(
        config.system_order_scope_evidence
    )
    try:
        verify_system_order_scope_evidence_parts(system_order_scope_acceptance)
    except BundleValidationError as exc:
        raise CollectorError(str(exc)) from exc
    _validate_system_order_scope_operator(
        system_order_scope_acceptance,
        config.system_order_scope_accepted_by,
    )
    if _operator_identities_match(
        config.reviewed_by,
        config.system_order_scope_accepted_by,
    ):
        raise CollectorError(
            "bundle.reviewed_by_must_differ_from_system_order_scope_accepted_by"
        )
    if not _operator_identity_groups_are_distinct(
        _provider_lifecycle_reviewer_identities(provider_lifecycle_evidence),
        _incident_channel_ack_operator_identities(incident_channel_evidence),
        _scope_acceptance_operator_identities(system_order_scope_acceptance),
    ):
        raise CollectorError("bundle.evidence_operator_roles_must_be_distinct")

    external_checks = {
        "hosted_supabase_live_readiness": _run_check(
            CommandSpec(
                name="hosted_supabase_live_readiness",
                command=(sys.executable, "supabase/verify_hosted_live_readiness.py"),
                cwd=config.repo_root,
                surface="hosted_supabase",
            ),
            runner,
            now,
        ),
        "hosted_live_enable_flow": _run_check(
            CommandSpec(
                name="hosted_live_enable_flow",
                command=(sys.executable, "supabase/verify_hosted_live_enable_flow.py"),
                cwd=config.repo_root,
                surface="hosted_supabase",
            ),
            runner,
            now,
        ),
        "provider_lifecycle_evidence": _run_check(
            CommandSpec(
                name="provider_lifecycle_evidence",
                command=(
                    sys.executable,
                    "-m",
                    "app.tools.verify_provider_lifecycle_evidence",
                    "--evidence",
                    str(config.provider_evidence),
                ),
                cwd=apps_worker,
                surface="toss_sandbox_or_live",
            ),
            runner,
            now,
        ),
        "live_incident_response_drill": {
            "surface": "real_incident_channel",
            "captured_at": _format_timestamp(now()),
            "final_output": incident_final_output,
            "channel_evidence": incident_channel_evidence,
        },
    }

    local_checks = {
        "worker_release_freshness": _run_check(
            CommandSpec(
                name="worker_release_freshness",
                command=(
                    sys.executable,
                    "-m",
                    "app.tools.verify_worker_release_freshness",
                    "--repo-root",
                    str(config.repo_root),
                ),
                cwd=apps_worker,
            ),
            runner,
            now,
        ),
        "live_enable_migration": _run_check(
            CommandSpec(
                name="live_enable_migration",
                command=(sys.executable, "supabase/verify_live_enable_migration.py"),
                cwd=config.repo_root,
            ),
            runner,
            now,
        ),
        "live_execution_safety_drill": _run_check(
            CommandSpec(
                name="live_execution_safety_drill",
                command=(
                    sys.executable,
                    "-m",
                    "app.tools.run_live_execution_safety_drill_once",
                ),
                cwd=apps_worker,
            ),
            runner,
            now,
        ),
        "live_recovery_drill": _run_check(
            CommandSpec(
                name="live_recovery_drill",
                command=(sys.executable, "-m", "app.tools.run_live_recovery_drill_once"),
                cwd=apps_worker,
            ),
            runner,
            now,
        ),
        "live_alert_drill": _run_check(
            CommandSpec(
                name="live_alert_drill",
                command=(sys.executable, "-m", "app.tools.run_live_alert_drill_once"),
                cwd=apps_worker,
            ),
            runner,
            now,
        ),
        "provider_contract_gaps": _run_check(
            CommandSpec(
                name="provider_contract_gaps",
                command=(
                    sys.executable,
                    "-m",
                    "app.tools.check_provider_contract_gaps",
                    "--system-order-scope-evidence",
                    str(config.system_order_scope_evidence),
                    "--provider-gap-evidence",
                    str(config.provider_gap_evidence),
                ),
                cwd=apps_worker,
            ),
            runner,
            now,
        ),
        "live_readiness_scorecard": _run_check(
            CommandSpec(
                name="live_readiness_scorecard",
                command=(
                    sys.executable,
                    "-m",
                    "app.tools.verify_live_readiness_scorecard",
                    "--scorecard",
                    str(config.repo_root / "docs" / "LIVE_READINESS_SCORECARD.md"),
                    "--security-evidence",
                    str(config.security_scan_summary),
                    "--repo-root",
                    str(config.repo_root),
                ),
                cwd=apps_worker,
            ),
            runner,
            now,
        ),
    }

    reviewed_at = now()
    generated_at = _bundle_generated_at(
        collection_started_at,
        provider_lifecycle_evidence,
        provider_gap_evidence,
        security_scan,
        incident_channel_evidence,
        system_order_scope_acceptance,
    )
    bundle: dict[str, object] = {
        "schema_version": 1,
        "environment": config.environment,
        "generated_at": _format_timestamp(generated_at),
        "reviewed_at": _format_timestamp(reviewed_at),
        "reviewed_by": config.reviewed_by,
        "external_checks": external_checks,
        "local_checks": local_checks,
        "provider_lifecycle_evidence": provider_lifecycle_evidence,
        "provider_gap_evidence": provider_gap_evidence,
        "system_order_scope_acceptance": system_order_scope_acceptance,
        "security_scan": security_scan,
    }

    try:
        summary = verify_live_readiness_evidence_bundle(bundle)
    except BundleValidationError as exc:
        raise CollectorError(str(exc)) from exc

    _write_collector_output(config.output, bundle)
    return bundle, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run live readiness gates, assemble a redacted evidence bundle, and validate it."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=_default_repo_root())
    parser.add_argument(
        "--environment",
        choices=("staging", "production-readiness"),
        default="staging",
    )
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument("--provider-evidence", required=True, type=Path)
    parser.add_argument("--provider-gap-evidence", required=True, type=Path)
    parser.add_argument("--incident-output-file", required=True, type=Path)
    parser.add_argument("--incident-channel-evidence", required=True, type=Path)
    parser.add_argument("--security-scan-summary", required=True, type=Path)
    parser.add_argument("--accept-system-order-scope", action="store_true")
    parser.add_argument("--system-order-scope-evidence", required=True, type=Path)
    parser.add_argument("--system-order-scope-accepted-by", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.accept_system_order_scope:
        parser.error("--accept-system-order-scope is required to record operator acceptance")

    config = CollectorConfig(
        repo_root=args.repo_root.resolve(),
        environment=args.environment,
        reviewed_by=args.reviewed_by,
        provider_evidence=args.provider_evidence.resolve(),
        provider_gap_evidence=args.provider_gap_evidence.resolve(),
        incident_output_file=args.incident_output_file.resolve(),
        incident_channel_evidence=args.incident_channel_evidence.resolve(),
        security_scan_summary=args.security_scan_summary.resolve(),
        system_order_scope_evidence=args.system_order_scope_evidence.resolve(),
        system_order_scope_accepted_by=args.system_order_scope_accepted_by,
        output=args.output.resolve(),
    )
    try:
        _, summary = collect_live_readiness_evidence_bundle(config)
    except (CollectorError, OSError) as exc:
        print(f"FINAL=FAIL live_readiness_evidence_collector reason={_safe_reason(str(exc))}")
        return 1

    print(
        "FINAL=PASS live_readiness_evidence_collector "
        f"external_checks={summary.external_checks} "
        f"local_checks={summary.local_checks} "
        "bundle_verified=1"
    )
    return 0


def _run_check(spec: CommandSpec, runner: CommandRunner, clock: Clock) -> dict[str, object]:
    result = runner(spec)
    if result.returncode != 0:
        raise CollectorError(f"{spec.name}_command_returncode_nonzero")
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    final_output = _extract_single_final_line(output, spec.name)
    check: dict[str, object] = {
        "captured_at": _format_timestamp(clock()),
        "final_output": final_output,
    }
    if spec.surface is not None:
        check["surface"] = spec.surface
    return check


def _run_command(spec: CommandSpec) -> CommandResult:
    completed = subprocess.run(
        list(spec.command),
        cwd=spec.cwd,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _extract_single_final_line(output: str, name: str) -> str:
    non_empty_lines = [line.strip() for line in output.splitlines() if line.strip()]
    final_lines = [
        line for line in non_empty_lines if line.startswith("FINAL=")
    ]
    if len(final_lines) != 1:
        raise CollectorError(f"{name}_final_line_count={len(final_lines)}")
    if len(non_empty_lines) != 1:
        raise CollectorError(f"{name}_non_final_output_lines_not_allowed")
    return final_lines[0]


def _load_security_scan_summary(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectorError("security_scan_summary_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise CollectorError("security_scan_summary_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise CollectorError("security_scan_summary_must_be_object")
    summary = cast(Mapping[str, object], payload)
    _scan_for_sensitive_keys(summary, "security_scan_summary")
    unknown = set(summary) - ALLOWED_SECURITY_SCAN_KEYS
    if unknown:
        raise CollectorError(f"security_scan_summary_unknown_keys={','.join(sorted(unknown))}")
    return {key: summary[key] for key in ALLOWED_SECURITY_SCAN_KEYS if key in summary}


def _load_provider_lifecycle_evidence(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectorError("provider_lifecycle_evidence_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise CollectorError("provider_lifecycle_evidence_json_invalid") from exc
    if not isinstance(payload, dict):
        raise CollectorError("provider_lifecycle_evidence_must_be_object")
    return cast(dict[str, object], payload)


def _load_provider_gap_evidence(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectorError("provider_gap_evidence_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise CollectorError("provider_gap_evidence_json_invalid") from exc
    if not isinstance(payload, dict):
        raise CollectorError("provider_gap_evidence_must_be_object")
    _scan_for_sensitive_keys(payload, "provider_gap_evidence")
    return cast(dict[str, object], payload)


def _validate_security_scan_git_binding(
    security_scan: Mapping[str, object],
    git_evidence: Mapping[str, str],
) -> None:
    for key in ("source_head", "source_diff_sha256"):
        expected = git_evidence[key]
        actual = security_scan.get(key)
        if actual != expected:
            raise CollectorError(f"security_scan_summary_{key}_mismatch")


def _security_scan_report_path(
    security_scan: Mapping[str, object],
    *,
    summary_path: Path,
) -> Path | None:
    return _resolve_security_scan_report_path(
        security_scan,
        base_dir=summary_path.parent,
    )


def _validate_collector_artifact_paths(*paths: Path) -> None:
    seen: set[str] = set()
    for path in paths:
        key = _canonical_collector_artifact_path(path)
        if key in seen:
            raise CollectorError("collector_artifact_paths_must_be_distinct")
        seen.add(key)


def _validate_collector_output_path(path: Path) -> None:
    if os.path.lexists(path):
        raise CollectorError("collector_output_path_must_not_exist")


def _write_collector_output(path: Path, bundle: Mapping[str, object]) -> None:
    content = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8") as output_file:
            output_file.write(content)
    except FileExistsError as exc:
        raise CollectorError("collector_output_path_must_not_exist") from exc
    except OSError as exc:
        raise CollectorError("collector_output_path_unwritable") from exc


def _canonical_collector_artifact_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _collect_git_evidence(
    repo_root: Path,
    *,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, str]:
    repo_root = _resolve_git_repo_root(repo_root)
    source_head = _run_git(repo_root, "rev-parse", "HEAD").decode("utf-8").strip()
    if not re.fullmatch(r"[A-Fa-f0-9]{40}", source_head):
        raise CollectorError("git_head_invalid")

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
    excluded = {path.resolve() for path in excluded_paths}
    for raw_rel_path in sorted(path for path in untracked.split(b"\0") if path):
        rel_path = raw_rel_path.decode("utf-8")
        full_path = (repo_root / rel_path).resolve()
        repo = repo_root.resolve()
        try:
            full_path.relative_to(repo)
        except ValueError as exc:
            raise CollectorError("git_untracked_path_outside_repo") from exc
        if full_path in excluded or not full_path.is_file():
            continue
        digest.update(b"\0untracked\0")
        digest.update(raw_rel_path)
        digest.update(b"\0")
        digest.update(full_path.read_bytes())
    return {
        "source_head": source_head,
        "source_diff_sha256": digest.hexdigest(),
    }


def _resolve_git_repo_root(repo_root: Path) -> Path:
    output = _run_git(repo_root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if not output:
        raise CollectorError("git_root_invalid")
    return Path(output).resolve()


def _repo_relative_excluded_paths(
    repo_root: Path,
    excluded_paths: Sequence[Path],
) -> list[str]:
    repo = repo_root.resolve()
    relative_paths: set[str] = set()
    for path in excluded_paths:
        try:
            relative_paths.add(path.resolve().relative_to(repo).as_posix())
        except ValueError:
            continue
    return sorted(relative_paths)


def _run_git(repo_root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise CollectorError(f"git_command_failed:{' '.join(args)}")
    return completed.stdout


def _load_incident_channel_evidence(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectorError("incident_channel_evidence_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise CollectorError("incident_channel_evidence_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise CollectorError("incident_channel_evidence_must_be_object")
    evidence = cast(Mapping[str, object], payload)
    _scan_for_sensitive_keys(evidence, "incident_channel_evidence")
    unknown = set(evidence) - ALLOWED_INCIDENT_CHANNEL_EVIDENCE_KEYS
    if unknown:
        raise CollectorError(f"incident_channel_evidence_unknown_keys={','.join(sorted(unknown))}")
    return {key: evidence[key] for key in ALLOWED_INCIDENT_CHANNEL_EVIDENCE_KEYS if key in evidence}


def _load_system_order_scope_evidence(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectorError("system_order_scope_evidence_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise CollectorError("system_order_scope_evidence_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise CollectorError("system_order_scope_evidence_must_be_object")
    evidence = cast(Mapping[str, object], payload)
    _scan_for_sensitive_keys(evidence, "system_order_scope_evidence")
    unknown = set(evidence) - ALLOWED_SYSTEM_ORDER_SCOPE_EVIDENCE_KEYS
    if unknown:
        raise CollectorError(
            f"system_order_scope_evidence_unknown_keys={','.join(sorted(unknown))}"
        )
    return {
        key: evidence[key] for key in ALLOWED_SYSTEM_ORDER_SCOPE_EVIDENCE_KEYS if key in evidence
    }


def _validate_system_order_scope_operator(
    acceptance: Mapping[str, object],
    accepted_by: str,
) -> None:
    evidence_accepted_by = acceptance.get("accepted_by")
    if evidence_accepted_by != accepted_by:
        raise CollectorError("system_order_scope_evidence_accepted_by_mismatch")


def _scan_for_sensitive_keys(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY_RE.search(key):
                raise CollectorError(f"sensitive_key_not_allowed:{child_path}")
            _scan_for_sensitive_keys(child, child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_sensitive_keys(child, f"{path}[{index}]")


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bundle_generated_at(
    collection_started_at: datetime,
    *evidence_sources: Mapping[str, object],
) -> datetime:
    candidates = [collection_started_at]
    for evidence in evidence_sources:
        for key in (
            "completed_at",
            "captured_at",
            "operator_ack_at",
            "accepted_at",
            "evidence_captured_at",
        ):
            raw_value = evidence.get(key)
            if not isinstance(raw_value, str):
                continue
            try:
                parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                candidates.append(parsed)
    return min(candidates)


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:700]


if __name__ == "__main__":
    raise SystemExit(main())

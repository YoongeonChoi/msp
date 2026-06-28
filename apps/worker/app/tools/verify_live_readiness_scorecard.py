from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.tools.verify_live_readiness_evidence_bundle import SecurityScanEvidenceSummary
from app.tools.verify_security_scan_evidence import (
    SecurityScanEvidenceValidationError,
    verify_security_scan_evidence_file,
)


class ScorecardValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScorecardValidationSummary:
    scan_id: str
    worklist_rows: int
    candidate_findings: int
    reportable_findings: int


def verify_live_readiness_scorecard_file(
    scorecard_path: Path,
    security_evidence_path: Path,
    *,
    repo_root: Path | None = None,
    runbook_path: Path | None = None,
) -> ScorecardValidationSummary:
    try:
        scorecard = scorecard_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScorecardValidationError("scorecard_file_unreadable") from exc

    try:
        security_summary = verify_security_scan_evidence_file(
            security_evidence_path,
            repo_root=repo_root,
        )
        evidence = _load_security_evidence(security_evidence_path)
    except SecurityScanEvidenceValidationError as exc:
        raise ScorecardValidationError(str(exc)) from exc

    reportable_findings = _require_non_negative_int(
        evidence,
        "reportable_findings",
        "security_scan_evidence",
    )
    report_path = _require_str(evidence, "report_path", "security_scan_evidence")

    errors: list[str] = []
    _validate_scorecard_security_row(
        scorecard,
        security_summary,
        reportable_findings,
        report_path,
        errors,
    )
    _validate_scorecard_metric_assignments(scorecard, security_summary, errors)
    resolved_runbook_path = runbook_path
    if resolved_runbook_path is None and repo_root is not None:
        resolved_runbook_path = repo_root / "docs" / "RUNBOOK.md"
    if resolved_runbook_path is not None:
        _validate_runbook_security_examples(
            resolved_runbook_path,
            security_summary,
            reportable_findings,
            errors,
        )

    if errors:
        raise ScorecardValidationError(";".join(errors))

    return ScorecardValidationSummary(
        scan_id=security_summary.scan_id,
        worklist_rows=security_summary.worklist_rows,
        candidate_findings=security_summary.candidate_findings,
        reportable_findings=reportable_findings,
    )


def _load_security_evidence(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SecurityScanEvidenceValidationError(
            "security_scan_evidence_unreadable"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SecurityScanEvidenceValidationError(
            "security_scan_evidence_json_invalid"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SecurityScanEvidenceValidationError("security_scan_evidence_must_be_object")
    return cast(Mapping[str, object], payload)


def _validate_scorecard_security_row(
    scorecard: str,
    security_summary: SecurityScanEvidenceSummary,
    reportable_findings: int,
    report_path: str,
    errors: list[str],
) -> None:
    row = _extract_scorecard_row(scorecard, "Security posture")
    if row is None:
        errors.append("scorecard.security_posture_row_missing")
        return
    if _contains_local_absolute_path(row):
        errors.append("scorecard.security_posture_report_path_must_not_be_local_absolute")

    expected_fragments = {
        "scan_id": security_summary.scan_id,
        "report_path": report_path,
        "worklist_rows": f"{security_summary.worklist_rows} worklist rows",
        "completion_receipts": (
            f"{security_summary.completion_receipts} completion receipts"
        ),
        "candidate_findings": (
            f"{security_summary.candidate_findings} promoted candidates"
        ),
        "validation_receipts": (
            f"{security_summary.validation_receipts} validation receipts"
        ),
        "attack_path_receipts": (
            f"{security_summary.attack_path_receipts} attack-path receipts"
        ),
        "reportable_findings": (
            f"{reportable_findings} surviving reportable findings"
        ),
    }
    for field, expected_fragment in expected_fragments.items():
        if expected_fragment not in row:
            errors.append(f"scorecard.security_posture_{field}_mismatch")


def _validate_scorecard_metric_assignments(
    scorecard: str,
    security_summary: SecurityScanEvidenceSummary,
    errors: list[str],
) -> None:
    expected_values = {
        "worklist_rows": security_summary.worklist_rows,
        "completion_receipts": security_summary.completion_receipts,
        "candidate_findings": security_summary.candidate_findings,
        "validation_receipts": security_summary.validation_receipts,
        "attack_path_receipts": security_summary.attack_path_receipts,
    }
    for metric_name, expected_value in expected_values.items():
        values = _assigned_int_values(scorecard, metric_name)
        if not values:
            errors.append(f"scorecard.{metric_name}_assignment_missing")
            continue
        if any(value != expected_value for value in values):
            errors.append(f"scorecard.{metric_name}_assignment_mismatch")


def _validate_runbook_security_examples(
    runbook_path: Path,
    security_summary: SecurityScanEvidenceSummary,
    reportable_findings: int,
    errors: list[str],
) -> None:
    try:
        runbook = runbook_path.read_text(encoding="utf-8")
    except OSError:
        errors.append("runbook_file_unreadable")
        return

    security_line = _extract_final_line(runbook, "security_scan_evidence")
    if security_line is None:
        errors.append("runbook.security_scan_evidence_example_missing")
    else:
        expected_security_metrics = {
            "scan_id": security_summary.scan_id,
            "worklist_rows": str(security_summary.worklist_rows),
            "completion_receipts": str(security_summary.completion_receipts),
            "candidate_findings": str(security_summary.candidate_findings),
            "validation_receipts": str(security_summary.validation_receipts),
            "attack_path_receipts": str(security_summary.attack_path_receipts),
        }
        for metric_name, expected_value in expected_security_metrics.items():
            if f"{metric_name}={expected_value}" not in security_line:
                errors.append(f"runbook.security_scan_evidence_{metric_name}_mismatch")

    scorecard_line = _extract_final_line(runbook, "live_readiness_scorecard")
    if scorecard_line is None:
        errors.append("runbook.live_readiness_scorecard_example_missing")
    else:
        expected_scorecard_metrics = {
            "scorecard_security_scan": "1",
            "worklist_rows": str(security_summary.worklist_rows),
            "candidate_findings": str(security_summary.candidate_findings),
            "reportable_findings": str(reportable_findings),
        }
        for metric_name, expected_value in expected_scorecard_metrics.items():
            if f"{metric_name}={expected_value}" not in scorecard_line:
                errors.append(f"runbook.live_readiness_scorecard_{metric_name}_mismatch")


def _extract_scorecard_row(scorecard: str, category: str) -> str | None:
    prefix = f"| {category} |"
    for line in scorecard.splitlines():
        if line.startswith(prefix):
            return line
    return None


def _extract_final_line(markdown: str, check_name: str) -> str | None:
    prefix = f"FINAL=PASS {check_name} "
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped
    return None


def _contains_local_absolute_path(value: str) -> bool:
    return re.search(r"\b[A-Za-z]:[/\\]", value) is not None


def _assigned_int_values(scorecard: str, metric_name: str) -> tuple[int, ...]:
    values: list[int] = []
    for match in re.finditer(rf"\b{re.escape(metric_name)}=(\d+)\b", scorecard):
        values.append(int(match.group(1)))
    return tuple(values)


def _require_str(evidence: Mapping[str, object], key: str, path: str) -> str:
    value = evidence.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SecurityScanEvidenceValidationError(f"{path}.{key}_must_be_non_empty_string")
    return value


def _require_non_negative_int(
    evidence: Mapping[str, object],
    key: str,
    path: str,
) -> int:
    value = evidence.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SecurityScanEvidenceValidationError(f"{path}.{key}_must_be_non_negative_int")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate that the live-readiness scorecard matches security scan evidence."
    )
    parser.add_argument("--scorecard", required=True, type=Path)
    parser.add_argument("--security-evidence", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--runbook",
        type=Path,
        default=None,
        help=(
            "Optional runbook markdown to verify against security evidence. "
            "Defaults to <repo-root>/docs/RUNBOOK.md when --repo-root is set."
        ),
    )
    args = parser.parse_args(argv)

    try:
        summary = verify_live_readiness_scorecard_file(
            args.scorecard,
            args.security_evidence,
            repo_root=args.repo_root,
            runbook_path=args.runbook,
        )
    except ScorecardValidationError as exc:
        print(f"FINAL=FAIL live_readiness_scorecard reason={_safe_reason(str(exc))}")
        return 1

    print(
        "FINAL=PASS live_readiness_scorecard "
        "scorecard_security_scan=1 "
        f"worklist_rows={summary.worklist_rows} "
        f"candidate_findings={summary.candidate_findings} "
        f"reportable_findings={summary.reportable_findings}"
    )
    return 0


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:700]


if __name__ == "__main__":
    raise SystemExit(main())

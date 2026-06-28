from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.tools.verify_live_readiness_scorecard import (
    ScorecardValidationError,
    main,
    verify_live_readiness_scorecard_file,
)


def test_live_readiness_scorecard_passes_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    security_evidence = _write_security_evidence(tmp_path)
    scorecard = _write_scorecard(tmp_path, _valid_security_summary())

    exit_code = main(
        [
            "--scorecard",
            str(scorecard),
            "--security-evidence",
            str(security_evidence),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS live_readiness_scorecard scorecard_security_scan=1 "
        "worklist_rows=46 candidate_findings=3 reportable_findings=0"
    ) in output


def test_live_readiness_scorecard_passes_with_current_runbook_examples(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    security_evidence = _write_security_evidence(tmp_path)
    summary = _valid_security_summary()
    scorecard = _write_scorecard(tmp_path, summary)
    runbook = _write_runbook(tmp_path, summary)

    exit_code = main(
        [
            "--scorecard",
            str(scorecard),
            "--security-evidence",
            str(security_evidence),
            "--runbook",
            str(runbook),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS live_readiness_scorecard" in output


def test_live_readiness_scorecard_rejects_stale_security_counts_without_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    security_evidence = _write_security_evidence(tmp_path)
    scorecard = _write_scorecard(
        tmp_path,
        _valid_security_summary()
        | {
            "worklist_rows": 45,
            "candidate_findings": 2,
            "report_path": "C:/Users/secret/security-report.md",
        },
    )

    exit_code = main(
        [
            "--scorecard",
            str(scorecard),
            "--security-evidence",
            str(security_evidence),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL live_readiness_scorecard" in output
    assert "scorecard.security_posture_report_path_must_not_be_local_absolute" in output
    assert "scorecard.security_posture_worklist_rows_mismatch" in output
    assert "scorecard.worklist_rows_assignment_mismatch" in output
    assert "C:/Users/secret" not in output


def test_live_readiness_scorecard_rejects_stale_runbook_examples_without_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    security_evidence = _write_security_evidence(tmp_path)
    summary = _valid_security_summary()
    scorecard = _write_scorecard(tmp_path, summary)
    runbook = _write_runbook(
        tmp_path,
        summary
        | {
            "worklist_rows": 45,
            "completion_receipts": 45,
            "candidate_findings": 2,
            "validation_receipts": 2,
            "attack_path_receipts": 2,
        },
    )

    exit_code = main(
        [
            "--scorecard",
            str(scorecard),
            "--security-evidence",
            str(security_evidence),
            "--runbook",
            str(runbook),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "runbook.security_scan_evidence_worklist_rows_mismatch" in output
    assert "runbook.security_scan_evidence_candidate_findings_mismatch" in output
    assert "runbook.live_readiness_scorecard_worklist_rows_mismatch" in output
    assert "runbook.live_readiness_scorecard_candidate_findings_mismatch" in output
    assert "45" not in output


def test_live_readiness_scorecard_requires_metric_assignments(tmp_path: Path) -> None:
    security_evidence = _write_security_evidence(tmp_path)
    summary = _valid_security_summary()
    scorecard = _write_scorecard(tmp_path, summary, include_assignments=False)

    with pytest.raises(ScorecardValidationError) as exc_info:
        verify_live_readiness_scorecard_file(scorecard, security_evidence)

    reason = str(exc_info.value)
    assert "scorecard.worklist_rows_assignment_missing" in reason
    assert "scorecard.candidate_findings_assignment_missing" in reason


def _write_security_evidence(tmp_path: Path) -> Path:
    report_path = tmp_path / "security-report.md"
    report_path.write_text("# Codex Security report\n\nNo findings.\n", encoding="utf-8")
    summary = _valid_security_summary()
    summary["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    path = tmp_path / "security-summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    return path


def _write_scorecard(
    tmp_path: Path,
    summary: dict[str, object],
    *,
    include_assignments: bool = True,
) -> Path:
    assignments = ""
    if include_assignments:
        assignments = (
            f"`worklist_rows={summary['worklist_rows']}`, "
            f"`completion_receipts={summary['completion_receipts']}`, "
            f"`candidate_findings={summary['candidate_findings']}`, "
            f"`validation_receipts={summary['validation_receipts']}`, "
            f"`attack_path_receipts={summary['attack_path_receipts']}`"
        )
    scorecard = tmp_path / "LIVE_READINESS_SCORECARD.md"
    scorecard.write_text(
        "\n".join(
            [
                "# Live Readiness Scorecard",
                "",
                "| Category | Current score | Evidence | Remaining gap before 100 |",
                "| --- | ---: | --- | --- |",
                (
                    "| Security posture | 99 | The current Codex Security working-tree "
                    f"scan `{summary['scan_id']}` is finalized at "
                    f"`{summary['report_path']}` with {summary['worklist_rows']} "
                    f"worklist rows, {summary['completion_receipts']} completion "
                    f"receipts, {summary['candidate_findings']} promoted candidates, "
                    f"{summary['validation_receipts']} validation receipts, "
                    f"{summary['attack_path_receipts']} attack-path receipts, and "
                    f"{summary['reportable_findings']} surviving reportable findings. "
                    "| Hosted evidence remains external. |"
                ),
                "",
                "## Next Strict Iteration",
                assignments,
            ]
        ),
        encoding="utf-8",
    )
    return scorecard


def _write_runbook(tmp_path: Path, summary: dict[str, object]) -> Path:
    runbook = tmp_path / "RUNBOOK.md"
    runbook.write_text(
        "\n".join(
            [
                "# Runbook",
                "",
                (
                    "FINAL=PASS security_scan_evidence "
                    f"scan_id={summary['scan_id']} "
                    f"worklist_rows={summary['worklist_rows']} "
                    f"completion_receipts={summary['completion_receipts']} "
                    f"candidate_findings={summary['candidate_findings']} "
                    f"validation_receipts={summary['validation_receipts']} "
                    f"attack_path_receipts={summary['attack_path_receipts']} "
                    "report_uri=https://..."
                ),
                (
                    "FINAL=PASS live_readiness_scorecard "
                    "scorecard_security_scan=1 "
                    f"worklist_rows={summary['worklist_rows']} "
                    f"candidate_findings={summary['candidate_findings']} "
                    f"reportable_findings={summary['reportable_findings']}"
                ),
            ]
        ),
        encoding="utf-8",
    )
    return runbook


def _valid_security_summary() -> dict[str, object]:
    return {
        "scan_id": "msp-20260628-independent-replay",
        "report_path": "security-report.md",
        "report_uri": "https://evidence.kr-autotrading.net/security-scans/msp-20260628/report.md",
        "report_sha256": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "source_head": "a" * 40,
        "source_diff_sha256": "b" * 64,
        "completed_at": "2026-06-28T01:08:00Z",
        "scan_profile": "security_diff_scan",
        "independent_replay": True,
        "threat_model_receipt": True,
        "finding_discovery_receipt": True,
        "worklist_rows": 46,
        "completion_receipts": 46,
        "candidate_findings": 3,
        "validation_receipts": 3,
        "attack_path_receipts": 3,
        "reportable_findings": 0,
    }

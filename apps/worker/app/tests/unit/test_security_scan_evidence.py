from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.tools.verify_security_scan_evidence import (
    SecurityScanEvidenceValidationError,
    main,
    verify_security_scan_evidence,
    verify_security_scan_report_uri_remote_fetch,
    verify_security_scan_report_uri_repo_artifact,
)


def test_security_scan_evidence_passes_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path, report_sha256 = _write_report(tmp_path)
    evidence_path = tmp_path / "security-scan-summary.json"
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                report_path=report_path,
                report_sha256=report_sha256,
            )
        ),
        encoding="utf-8",
    )

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS security_scan_evidence "
        "scan_id=msp-20260628-independent-replay "
        "worklist_rows=46 completion_receipts=46 "
        "candidate_findings=3 validation_receipts=3 attack_path_receipts=3 "
        "report_uri=https://evidence.kr-autotrading.net/security-scans/msp-20260628/report.md"
    ) in output


def test_security_scan_evidence_cli_rejects_report_sha_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path, _ = _write_report(tmp_path)
    evidence_path = tmp_path / "security-scan-summary.json"
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                report_path=report_path,
                report_sha256="f" * 64,
            )
        ),
        encoding="utf-8",
    )

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL security_scan_evidence" in output
    assert "security_scan_evidence.report_sha256_mismatch" in output


def test_security_scan_evidence_accepts_github_report_uri_repo_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "security-artifacts" / "msp-20260628" / "report.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Codex Security report\n\nNo findings.\n", encoding="utf-8")
    evidence = _valid_evidence(
        report_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    evidence["report_uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/"
        "security-artifacts/msp-20260628/report.md"
    )

    verify_security_scan_report_uri_repo_artifact(evidence, repo_root=tmp_path)


def test_security_scan_evidence_rejects_missing_github_report_uri_repo_artifact(
    tmp_path: Path,
) -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/"
        "security-artifacts/msp-20260628/report.md"
    )

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_report_uri_repo_artifact(evidence, repo_root=tmp_path)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_github_artifact_unreadable" in reason
    assert "security-artifacts" not in reason


def test_security_scan_evidence_rejects_github_report_uri_repo_artifact_hash_mismatch(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "security-artifacts" / "msp-20260628" / "report.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Codex Security report\n\nNo findings.\n", encoding="utf-8")
    evidence = _valid_evidence(report_sha256="f" * 64)
    evidence["report_uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/"
        "security-artifacts/msp-20260628/report.md"
    )

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_report_uri_repo_artifact(evidence, repo_root=tmp_path)

    reason = str(exc_info.value)
    assert (
        "security_scan_evidence.report_uri_github_artifact_sha256_mismatch"
        in reason
    )
    assert "security-artifacts" not in reason


def test_security_scan_evidence_cli_rejects_unpublished_github_report_uri_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path, report_sha256 = _write_report(tmp_path)
    evidence = _valid_evidence(
        report_path=report_path,
        report_sha256=report_sha256,
    )
    evidence["report_uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/"
        "security-artifacts/msp-20260628/report.md"
    )
    evidence_path = tmp_path / "security-scan-summary.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--repo-root",
            str(tmp_path),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL security_scan_evidence" in output
    assert "security_scan_evidence.report_uri_github_artifact_unreadable" in output
    assert "security-artifacts" not in output


def test_security_scan_evidence_accepts_remote_report_uri_hash_match() -> None:
    report_body = b"# Codex Security report\n\nNo findings.\n"
    evidence = _valid_evidence(
        report_sha256=hashlib.sha256(report_body).hexdigest(),
    )

    verify_security_scan_report_uri_remote_fetch(
        evidence,
        fetcher=lambda _uri, _timeout: report_body,
    )


def test_security_scan_evidence_rejects_remote_report_uri_hash_mismatch() -> None:
    evidence = _valid_evidence(report_sha256="f" * 64)

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_report_uri_remote_fetch(
            evidence,
            fetcher=lambda _uri, _timeout: b"# different report\n",
        )

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_remote_sha256_mismatch" in reason
    assert "different report" not in reason


def test_security_scan_evidence_rejects_github_blob_remote_release_uri() -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/"
        "security-artifacts/msp-20260628/report.md"
    )

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_report_uri_remote_fetch(
            evidence,
            fetcher=lambda _uri, _timeout: b"unused",
        )

    reason = str(exc_info.value)
    assert (
        "security_scan_evidence.report_uri_remote_must_reference_raw_report_bytes"
        in reason
    )
    assert "github.com" not in reason


def test_security_scan_evidence_cli_rejects_remote_fetch_failure_without_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, report_sha256 = _write_report(tmp_path)
    evidence_path = tmp_path / "security-scan-summary.json"
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                report_path=report_path,
                report_sha256=report_sha256,
            )
        ),
        encoding="utf-8",
    )

    def raise_os_error(_uri: str, _timeout: int) -> bytes:
        raise OSError("secret signed URL must not leak")

    monkeypatch.setattr(
        "app.tools.verify_security_scan_evidence._default_remote_report_fetcher",
        raise_os_error,
    )

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--verify-remote-report-uri",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL security_scan_evidence" in output
    assert "security_scan_evidence.report_uri_remote_fetch_failed" in output
    assert "secret signed URL" not in output
    assert "evidence.kr-autotrading.net" not in output


def test_security_scan_evidence_cli_rejects_absolute_report_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path, report_sha256 = _write_report(tmp_path)
    evidence_path = tmp_path / "security-scan-summary.json"
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                report_path=tmp_path / report_path,
                report_sha256=report_sha256,
            )
        ),
        encoding="utf-8",
    )

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL security_scan_evidence" in output
    assert "security_scan_evidence.report_path_must_be_relative_retained_path" in output
    assert str(tmp_path) not in output


def test_security_scan_evidence_cli_rejects_report_path_escape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    report = outside / "report.md"
    report.write_text("# Codex Security report\n\nNo findings.\n", encoding="utf-8")
    evidence_path = tmp_path / "security-scan-summary.json"
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                report_path=Path("..") / "outside" / "report.md",
                report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
            )
        ),
        encoding="utf-8",
    )

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL security_scan_evidence" in output
    assert "security_scan_evidence.report_path_must_stay_under_evidence_dir" in output
    assert "outside" not in output


def test_security_scan_evidence_rejects_weak_report_artifact() -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = "https://example.test/mock-security-report?token=abc"
    evidence["report_sha256"] = "not-a-sha"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_must_not_be_mock_fixture_or_local" in reason
    assert (
        "security_scan_evidence.report_uri_must_not_include_query_or_fragment"
        in reason
    )
    assert "security_scan_evidence.report_sha256_must_be_64_hex" in reason
    assert "token=abc" not in reason


def test_security_scan_evidence_rejects_path_like_scan_id_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["scan_id"] = "C:security-scan-20260628"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.scan_id_must_be_logical_identifier" in reason
    assert "C:security-scan-20260628" not in reason


def test_security_scan_evidence_rejects_absolute_report_path_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["report_path"] = "C:/Users/choey/.tmp/security/report.md"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_path_must_be_relative_retained_path" in reason
    assert "C:/Users/choey" not in reason


def test_security_scan_evidence_rejects_escaping_report_path_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["report_path"] = "../outside/report.md"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_path_must_stay_under_evidence_dir" in reason
    assert "outside" not in reason


def test_security_scan_evidence_rejects_non_markdown_report_artifact() -> None:
    evidence = _valid_evidence()
    evidence["report_path"] = "security-scan-summary.json"
    evidence["report_uri"] = "https://evidence.kr-autotrading.net/security-scans/msp-20260628/summary.json"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_path_must_be_markdown_report" in reason
    assert "security_scan_evidence.report_uri_must_reference_markdown_report" in reason
    assert "summary.json" not in reason


def test_security_scan_evidence_rejects_non_https_report_uri() -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = "s3://ops-internal/security-scans/msp/report.md"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_must_be_https_uri" in reason
    assert "ops-internal/security-scans" not in reason


def test_security_scan_evidence_rejects_report_uri_path_traversal_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = (
        "https://evidence.kr-autotrading.net/security-scans/"
        "msp-20260628/%252e%252e/report.md"
    )

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert (
        "security_scan_evidence.report_uri_must_not_include_path_traversal"
        in reason
    )
    assert "%252e%252e" not in reason


def test_security_scan_evidence_rejects_local_https_report_uri() -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = "https://10.0.0.5/security-scans/msp/report.md"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_must_be_remote_retained_uri" in reason
    assert "10.0.0.5" not in reason


def test_security_scan_evidence_rejects_non_global_https_report_ip_host() -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = "https://100.64.0.5/security-scans/msp/report.md"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_must_be_remote_retained_uri" in reason
    assert "100.64.0.5" not in reason


def test_security_scan_evidence_rejects_invalid_dns_report_host() -> None:
    evidence = _valid_evidence()
    evidence["report_uri"] = "https://ops_.internal/security-scans/msp/report.md"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_must_be_remote_retained_uri" in reason
    assert "ops_.internal" not in reason


def test_security_scan_evidence_rejects_incomplete_replay() -> None:
    evidence = _valid_evidence()
    evidence["independent_replay"] = False
    evidence["threat_model_receipt"] = False
    evidence["finding_discovery_receipt"] = False
    evidence["completion_receipts"] = 47
    evidence["validation_receipts"] = 2
    evidence["attack_path_receipts"] = 1
    evidence["reportable_findings"] = 1

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.independent_replay_must_be_true" in reason
    assert "security_scan_evidence.threat_model_receipt_must_be_true" in reason
    assert "security_scan_evidence.finding_discovery_receipt_must_be_true" in reason
    assert "security_scan_evidence.completion_receipts_must_equal_worklist_rows" in reason
    assert (
        "security_scan_evidence.validation_receipts_must_equal_candidate_findings"
        in reason
    )
    assert (
        "security_scan_evidence.attack_path_receipts_must_equal_candidate_findings"
        in reason
    )
    assert "security_scan_evidence.reportable_findings_must_be_0" in reason


def test_security_scan_evidence_rejects_future_completed_at() -> None:
    evidence = _valid_evidence()
    evidence["completed_at"] = "2099-06-28T01:08:00Z"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "security_scan_evidence.completed_at_must_not_be_future" in reason
    assert "2099" not in reason


def test_security_scan_evidence_rejects_wrong_scan_profile() -> None:
    evidence = _valid_evidence()
    evidence["scan_profile"] = "summary_only"

    with pytest.raises(
        SecurityScanEvidenceValidationError,
        match="security_scan_evidence.scan_profile_must_be_security_diff_scan",
    ):
        verify_security_scan_evidence(evidence)


def test_security_scan_evidence_rejects_sensitive_unknown_key_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["operator_token"] = "security-secret-that-must-not-print"

    with pytest.raises(SecurityScanEvidenceValidationError) as exc_info:
        verify_security_scan_evidence(evidence)

    reason = str(exc_info.value)
    assert "sensitive_key_not_allowed:security_scan_evidence.operator_token" in reason
    assert "security_scan_evidence.unknown_keys=operator_token" in reason
    assert "security-secret-that-must-not-print" not in reason


def test_security_scan_evidence_cli_rejects_stale_source_binding_without_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ops@example.test")
    _git(repo, "config", "user.name", "Ops Test")
    source_file = repo / "tracked.py"
    source_file.write_text("print('stable')\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")

    report_path, report_sha256 = _write_report(repo)
    evidence_path = repo / "security-scan-summary.json"
    stale_diff_sha256 = "f" * 64
    evidence = _valid_evidence(
        report_path=report_path,
        report_sha256=report_sha256,
    )
    evidence["source_head"] = _git(repo, "rev-parse", "HEAD").strip()
    evidence["source_diff_sha256"] = stale_diff_sha256
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--repo-root",
            str(repo),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL security_scan_evidence" in output
    assert "security_scan_evidence.source_diff_sha256_mismatch" in output
    assert stale_diff_sha256 not in output


def test_security_scan_evidence_cli_handles_source_binding_failure_without_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, report_sha256 = _write_report(tmp_path)
    evidence_path = tmp_path / "security-scan-summary.json"
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                report_path=report_path,
                report_sha256=report_sha256,
            )
        ),
        encoding="utf-8",
    )

    def raise_os_error(_repo_root: Path, **_kwargs: object) -> dict[str, str]:
        raise OSError("secret local path must not leak")

    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._collect_security_source_binding",
        raise_os_error,
    )

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--repo-root",
            str(tmp_path),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL security_scan_evidence" in output
    assert "security_scan_evidence.source_binding_unavailable" in output
    assert "secret local path" not in output


def _valid_evidence(
    *,
    report_path: Path | None = None,
    report_sha256: str | None = None,
) -> dict[str, object]:
    return copy.deepcopy(
        {
            "scan_id": "msp-20260628-independent-replay",
            "report_path": (
                str(report_path)
                if report_path is not None
                else "report.md"
            ),
            "report_uri": "https://evidence.kr-autotrading.net/security-scans/msp-20260628/report.md",
            "report_sha256": report_sha256
            or (
                "1234567890abcdef1234567890abcdef"
                "1234567890abcdef1234567890abcdef"
            ),
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
    )


def _write_report(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "report.md"
    content = "# Codex Security report\n\nNo reportable findings.\n"
    path.write_text(content, encoding="utf-8")
    return Path(path.name), hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout

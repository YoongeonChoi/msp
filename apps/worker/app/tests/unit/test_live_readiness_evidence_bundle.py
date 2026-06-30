from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from app.application.services.provider_gap_gate import (
    evaluate_provider_api_gaps,
    provider_api_gap_id,
)
from app.tools.verify_live_readiness_evidence_bundle import (
    BundleValidationError,
    main,
    verify_live_readiness_evidence_bundle,
    verify_live_readiness_evidence_bundle_file,
)


def test_live_readiness_evidence_bundle_passes_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _valid_bundle()
    evidence_path = _write_evidence(tmp_path, bundle)
    security_scan = cast(dict[str, object], bundle["security_scan"])
    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._collect_security_source_binding",
        lambda repo_root, *, excluded_paths=(): {
            "source_head": str(security_scan["source_head"]),
            "source_diff_sha256": str(security_scan["source_diff_sha256"]),
        },
    )

    exit_code = main(["--evidence", str(evidence_path), "--repo-root", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS live_readiness_evidence_bundle "
        "external_checks=4 local_checks=7 security_scan=1 "
        "system_order_scope_accepted=1 provider_gap_evidence=1 "
        "remote_provider_artifacts=0 remote_incident_evidence=0 "
        "remote_system_order_scope_evidence=0"
    ) in output


def test_live_readiness_evidence_bundle_file_verifies_remote_provider_artifacts(
    tmp_path: Path,
) -> None:
    bundle = _valid_bundle()
    bodies = _attach_provider_remote_artifact_hashes(bundle)
    evidence_path = _write_evidence(tmp_path, bundle)
    calls: list[tuple[str, int]] = []

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        calls.append((uri, timeout_seconds))
        return bodies[uri]

    summary = verify_live_readiness_evidence_bundle_file(
        evidence_path,
        verify_remote_provider_artifacts=True,
        remote_provider_artifact_fetcher=fetcher,
        remote_provider_artifact_timeout_seconds=4,
    )

    assert summary.external_checks == 4
    assert summary.remote_provider_artifacts is True
    assert summary.remote_incident_evidence is False
    assert summary.remote_system_order_scope_evidence is False
    assert len(calls) == 5
    assert {timeout for _, timeout in calls} == {4}


def test_live_readiness_evidence_bundle_cli_verifies_remote_provider_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _valid_bundle()
    bodies = _attach_provider_remote_artifact_hashes(bundle)
    evidence_path = _write_evidence(tmp_path, bundle)
    security_scan = cast(dict[str, object], bundle["security_scan"])
    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._collect_security_source_binding",
        lambda repo_root, *, excluded_paths=(): {
            "source_head": str(security_scan["source_head"]),
            "source_diff_sha256": str(security_scan["source_diff_sha256"]),
        },
    )

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        assert timeout_seconds == 10
        return bodies[uri]

    monkeypatch.setattr(
        "app.tools.verify_provider_lifecycle_evidence._default_remote_artifact_fetcher",
        fetcher,
    )

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--repo-root",
            str(tmp_path),
            "--verify-remote-provider-artifacts",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS live_readiness_evidence_bundle" in output
    assert "remote_provider_artifacts=1" in output
    assert "remote_incident_evidence=0" in output
    assert "remote_system_order_scope_evidence=0" in output


def test_live_readiness_evidence_bundle_remote_provider_artifact_failure_is_scoped(
    tmp_path: Path,
) -> None:
    bundle = _valid_bundle()
    bodies = _attach_provider_remote_artifact_hashes(bundle)
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    artifacts = cast(list[dict[str, object]], provider_evidence["evidence_artifacts"])
    artifacts[0]["sha256"] = _real_sha256(b"different-provider-artifact")
    evidence_path = _write_evidence(tmp_path, bundle)

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        return bodies[uri]

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle_file(
            evidence_path,
            verify_remote_provider_artifacts=True,
            remote_provider_artifact_fetcher=fetcher,
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.evidence_artifacts[0].uri_remote_sha256_mismatch"
        in reason
    )
    assert "provider-bundle-artifact-0" not in reason
    assert "order" not in reason


def test_live_readiness_evidence_bundle_file_verifies_remote_incident_and_scope_evidence(
    tmp_path: Path,
) -> None:
    bundle = _valid_bundle()
    bodies: dict[str, bytes] = {}
    bodies.update(_attach_incident_remote_evidence_hash(bundle))
    bodies.update(_attach_system_scope_remote_evidence_hash(bundle))
    evidence_path = _write_evidence(tmp_path, bundle)
    calls: list[tuple[str, int]] = []

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        calls.append((uri, timeout_seconds))
        return bodies[uri]

    summary = verify_live_readiness_evidence_bundle_file(
        evidence_path,
        verify_remote_incident_evidence=True,
        remote_incident_evidence_fetcher=fetcher,
        remote_incident_evidence_timeout_seconds=4,
        verify_remote_system_order_scope_evidence=True,
        remote_system_order_scope_evidence_fetcher=fetcher,
        remote_system_order_scope_evidence_timeout_seconds=5,
    )

    assert summary.system_order_scope_accepted is True
    assert summary.remote_provider_artifacts is False
    assert summary.remote_incident_evidence is True
    assert summary.remote_system_order_scope_evidence is True
    assert len(calls) == 2
    assert {timeout for _, timeout in calls} == {4, 5}


def test_live_readiness_evidence_bundle_cli_verifies_remote_incident_and_scope_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _valid_bundle()
    bodies: dict[str, bytes] = {}
    bodies.update(_attach_incident_remote_evidence_hash(bundle))
    bodies.update(_attach_system_scope_remote_evidence_hash(bundle))
    evidence_path = _write_evidence(tmp_path, bundle)
    security_scan = cast(dict[str, object], bundle["security_scan"])
    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._collect_security_source_binding",
        lambda repo_root, *, excluded_paths=(): {
            "source_head": str(security_scan["source_head"]),
            "source_diff_sha256": str(security_scan["source_diff_sha256"]),
        },
    )

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        assert timeout_seconds == 10
        return bodies[uri]

    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._default_remote_evidence_fetcher",
        fetcher,
    )

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--repo-root",
            str(tmp_path),
            "--verify-remote-incident-evidence",
            "--verify-remote-system-order-scope-evidence",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS live_readiness_evidence_bundle" in output
    assert "remote_provider_artifacts=0" in output
    assert "remote_incident_evidence=1" in output
    assert "remote_system_order_scope_evidence=1" in output


def test_live_readiness_evidence_bundle_cli_reports_all_remote_verification_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _valid_bundle()
    bodies: dict[str, bytes] = {}
    bodies.update(_attach_provider_remote_artifact_hashes(bundle))
    bodies.update(_attach_incident_remote_evidence_hash(bundle))
    bodies.update(_attach_system_scope_remote_evidence_hash(bundle))
    evidence_path = _write_evidence(tmp_path, bundle)
    security_scan = cast(dict[str, object], bundle["security_scan"])
    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._collect_security_source_binding",
        lambda repo_root, *, excluded_paths=(): {
            "source_head": str(security_scan["source_head"]),
            "source_diff_sha256": str(security_scan["source_diff_sha256"]),
        },
    )

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        assert timeout_seconds == 10
        return bodies[uri]

    monkeypatch.setattr(
        "app.tools.verify_provider_lifecycle_evidence._default_remote_artifact_fetcher",
        fetcher,
    )
    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._default_remote_evidence_fetcher",
        fetcher,
    )

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--repo-root",
            str(tmp_path),
            "--verify-remote-provider-artifacts",
            "--verify-remote-incident-evidence",
            "--verify-remote-system-order-scope-evidence",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS live_readiness_evidence_bundle "
        "external_checks=4 local_checks=7 security_scan=1 "
        "system_order_scope_accepted=1 provider_gap_evidence=1 "
        "remote_provider_artifacts=1 remote_incident_evidence=1 "
        "remote_system_order_scope_evidence=1"
    ) in output


def test_live_readiness_evidence_bundle_remote_incident_failure_is_scoped(
    tmp_path: Path,
) -> None:
    bundle = _valid_bundle()
    bodies = _attach_incident_remote_evidence_hash(bundle)
    incident = _external_check(bundle, "live_incident_response_drill")
    channel_evidence = cast(dict[str, object], incident["channel_evidence"])
    channel_evidence["evidence_sha256"] = _real_sha256(b"different-incident-export")
    evidence_path = _write_evidence(tmp_path, bundle)

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        return bodies[uri]

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle_file(
            evidence_path,
            verify_remote_incident_evidence=True,
            remote_incident_evidence_fetcher=fetcher,
        )

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "evidence_uri_remote_sha256_mismatch"
    ) in reason
    assert "incident-bundle-evidence" not in reason
    assert "INC-20260628-1" not in reason


def test_live_readiness_evidence_bundle_remote_scope_failure_is_scoped(
    tmp_path: Path,
) -> None:
    bundle = _valid_bundle()
    bodies = _attach_system_scope_remote_evidence_hash(bundle)
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["evidence_sha256"] = _real_sha256(b"different-scope-approval")
    evidence_path = _write_evidence(tmp_path, bundle)

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        return bodies[uri]

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle_file(
            evidence_path,
            verify_remote_system_order_scope_evidence=True,
            remote_system_order_scope_evidence_fetcher=fetcher,
        )

    reason = str(exc_info.value)
    assert "system_order_scope_acceptance.evidence_uri_remote_sha256_mismatch" in reason
    assert "system-scope-bundle-evidence" not in reason
    assert "SCOPE-20260628-1" not in reason


def test_live_readiness_evidence_bundle_cli_rejects_stale_source_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _valid_bundle()
    evidence_path = _write_evidence(tmp_path, bundle)
    stale_diff = "f" * 64
    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._collect_security_source_binding",
        lambda repo_root, *, excluded_paths=(): {
            "source_head": str(cast(dict[str, object], bundle["security_scan"])["source_head"]),
            "source_diff_sha256": stale_diff,
        },
    )

    exit_code = main(["--evidence", str(evidence_path), "--repo-root", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL live_readiness_evidence_bundle" in output
    assert "security_scan.source_diff_sha256_mismatch" in output
    assert stale_diff not in output


def test_live_readiness_evidence_bundle_cli_handles_source_binding_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _valid_bundle()
    evidence_path = _write_evidence(tmp_path, bundle)

    def raise_os_error(_repo_root: Path, **_kwargs: object) -> dict[str, str]:
        raise OSError("secret source path must not leak")

    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._collect_security_source_binding",
        raise_os_error,
    )

    exit_code = main(["--evidence", str(evidence_path), "--repo-root", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL live_readiness_evidence_bundle" in output
    assert "security_scan.source_binding_unavailable" in output
    assert "secret source path" not in output


def test_live_readiness_evidence_bundle_cli_rejects_security_report_path_escape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    outside_dir = tmp_path / "outside"
    evidence_dir.mkdir()
    outside_dir.mkdir()
    report_path = outside_dir / "report.md"
    report_path.write_text("# Codex Security report\n\nNo findings.\n", encoding="utf-8")
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_path"] = "../outside/report.md"
    security_scan["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    evidence_path = evidence_dir / "live_readiness_evidence_bundle.json"
    evidence_path.write_text(json.dumps(bundle), encoding="utf-8")

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL live_readiness_evidence_bundle" in output
    assert "security_scan.report_path_must_stay_under_evidence_dir" in output
    assert "outside" not in output


def test_live_readiness_evidence_bundle_rejects_scope_environment_mismatch() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["deployment_environment"] = "production"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "system_order_scope_acceptance.deployment_environment_must_match_bundle_environment"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_requires_production_scope_for_readiness() -> None:
    bundle = _valid_bundle()
    bundle["environment"] = "production-readiness"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "system_order_scope_acceptance.deployment_environment_must_match_bundle_environment"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_provider_lifecycle_environment_mismatch() -> None:
    bundle = _valid_bundle()
    bundle["environment"] = "production-readiness"
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["deployment_environment"] = "production"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "external_checks.provider_lifecycle_evidence.environment_must_match_bundle_environment"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_accepts_production_readiness_live_evidence() -> None:
    bundle = _valid_bundle()
    bundle["environment"] = "production-readiness"
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["deployment_environment"] = "production"
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    provider_evidence["environment"] = "live"
    provider = _external_check(bundle, "provider_lifecycle_evidence")
    provider["final_output"] = str(provider["final_output"]).replace(
        "environment=sandbox",
        "environment=live",
    )

    summary = verify_live_readiness_evidence_bundle(bundle)

    assert summary.system_order_scope_accepted is True


def test_live_readiness_evidence_bundle_rejects_automated_scope_acceptance_operator() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["accepted_by"] = "scope-ci-bot"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "system_order_scope_acceptance.accepted_by_must_be_human" in reason
    assert "scope-ci-bot" not in reason


def test_live_readiness_evidence_bundle_rejects_email_like_scope_acceptance_operator() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["accepted_by"] = "ops-admin@example.com"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "system_order_scope_acceptance.accepted_by_must_be_logical_operator_id" in reason
    assert "ops-admin@example.com" not in reason


def test_live_readiness_evidence_bundle_rejects_scope_capture_not_after_acceptance() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["accepted_at"] = "2026-06-28T01:09:00Z"
    acceptance["evidence_captured_at"] = "2026-06-28T01:09:00Z"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "system_order_scope_acceptance.evidence_captured_at_must_be_after_accepted_at"
        in reason
    )
    assert "2026-06-28T01" not in reason


def test_live_readiness_evidence_bundle_rejects_automated_bundle_reviewer() -> None:
    bundle = _valid_bundle()
    bundle["reviewed_by"] = "release-github-actions-bot"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.reviewed_by_must_be_human" in reason
    assert "release-github-actions-bot" not in reason


def test_live_readiness_evidence_bundle_rejects_contact_like_bundle_reviewer() -> None:
    bundle = _valid_bundle()
    bundle["reviewed_by"] = "release-admin@example.com"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.reviewed_by_must_be_logical_operator_id" in reason
    assert "release-admin@example.com" not in reason


def test_live_readiness_evidence_bundle_rejects_scope_acceptance_self_review() -> None:
    bundle = _valid_bundle()
    bundle["reviewed_by"] = "ops-admin-1"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.reviewed_by_must_differ_from_system_order_scope_accepted_by" in reason
    assert "ops-admin-1" not in reason


def test_live_readiness_evidence_bundle_rejects_incident_ack_self_review() -> None:
    bundle = _valid_bundle()
    bundle["reviewed_by"] = "ops-admin-2"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.reviewed_by_must_differ_from_incident_ack_operator" in reason
    assert "ops-admin-2" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_lifecycle_self_review() -> None:
    bundle = _valid_bundle()
    bundle["reviewed_by"] = "provider-admin-1"
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["accepted_by"] = "scope-admin-1"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.reviewed_by_must_differ_from_provider_lifecycle_reviewer" in reason
    assert "provider-admin-1" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_lifecycle_internal_self_review() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    audit = cast(dict[str, object], provider_evidence["audit"])
    audit["reviewed_by"] = "provider.admin.1"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.operator_reviewed_by_must_differ_from_audit_reviewed_by"
        in reason
    )
    assert "provider.admin.1" not in reason
    assert "provider-admin-1" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_lifecycle_audit_before_recovery() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    audit = cast(dict[str, object], provider_evidence["audit"])
    audit["reviewed_at"] = "2026-06-28T01:06:30Z"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "audit.reviewed_at_must_be_after_unknown_recovery_operator_reviewed_at"
        in reason
    )
    assert "2026-06-28T01" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_recovery_before_latest_status() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    unknown_recovery = cast(dict[str, object], provider_evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_at"] = "2026-06-28T01:04:15Z"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.operator_reviewed_at_must_be_after_latest_provider_status_observed_at"
        in reason
    )
    assert "2026-06-28T01" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_status_regression_after_terminal() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    status_sequence = cast(list[dict[str, object]], provider_evidence["provider_status_sequence"])
    status_sequence.append(
        {
            "observed_at": "2026-06-28T01:05:30Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "PENDING",
            "local_status": "sent",
        }
    )
    artifacts = cast(list[dict[str, object]], provider_evidence["evidence_artifacts"])
    artifacts[1]["captured_at"] = "2026-06-28T01:06:00Z"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "provider_status_sequence[2].provider_status_must_not_regress_after_terminal"
        in reason
    )
    assert "PENDING" not in reason


def test_live_readiness_evidence_bundle_rejects_local_status_regression_after_terminal() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    status_sequence = cast(list[dict[str, object]], provider_evidence["provider_status_sequence"])
    status_sequence.append(
        {
            "observed_at": "2026-06-28T01:05:30Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "CANCELED",
            "local_status": "sent",
        }
    )
    artifacts = cast(list[dict[str, object]], provider_evidence["evidence_artifacts"])
    artifacts[1]["captured_at"] = "2026-06-28T01:06:00Z"
    artifacts[2]["captured_at"] = "2026-06-28T01:06:00Z"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "provider_status_sequence[2].local_status_must_not_regress_after_terminal"
        in reason
    )
    assert "sent" not in reason


def test_live_readiness_evidence_bundle_rejects_create_status_first_observation_mismatch() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    created_order = cast(dict[str, object], provider_evidence["created_order"])
    created_order["status_after_create"] = "unknown_requires_manual_check"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "created_order.status_after_create_must_match_first_local_status"
        in reason
    )
    assert "unknown_requires_manual_check" not in reason


def test_live_readiness_evidence_bundle_rejects_unknown_recovery_for_different_order() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    unknown_recovery = cast(dict[str, object], provider_evidence["unknown_recovery"])
    unknown_recovery["order_id"] = "99999999-9999-4999-8999-999999999999"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.order_id_must_match_created_order"
        in reason
    )
    assert "99999999" not in reason


def test_live_readiness_evidence_bundle_rejects_unknown_recovery_final_status_mismatch() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    unknown_recovery = cast(dict[str, object], provider_evidence["unknown_recovery"])
    unknown_recovery["final_status"] = "filled"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.final_status_must_match_latest_local_status"
        in reason
    )
    assert "filled" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_incident_operator_reuse() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    channel_evidence = cast(dict[str, object], incident["channel_evidence"])
    channel_evidence["operator_ack_by"] = "provider-admin-1"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.evidence_operator_roles_must_be_distinct" in reason
    assert "provider-admin-1" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_scope_operator_reuse() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["accepted_by"] = "provider-admin-1"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.evidence_operator_roles_must_be_distinct" in reason
    assert "provider-admin-1" not in reason


def test_live_readiness_evidence_bundle_rejects_incident_scope_operator_reuse() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    channel_evidence = cast(dict[str, object], incident["channel_evidence"])
    channel_evidence["operator_ack_by"] = "ops-admin-1"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.evidence_operator_roles_must_be_distinct" in reason
    assert "ops-admin-1" not in reason


def test_live_readiness_evidence_bundle_rejects_future_bundle_timestamps() -> None:
    bundle = _valid_bundle()
    bundle["generated_at"] = "2099-06-28T01:00:00Z"
    bundle["reviewed_at"] = "2099-06-28T01:20:00Z"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.generated_at_must_not_be_future" in reason
    assert "bundle.reviewed_at_must_not_be_future" in reason
    assert "2099" not in reason


def test_live_readiness_evidence_bundle_requires_provider_lifecycle_payload() -> None:
    bundle = _valid_bundle()
    del bundle["provider_lifecycle_evidence"]

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "bundle.provider_lifecycle_evidence_must_be_object" in str(exc_info.value)


def test_live_readiness_evidence_bundle_revalidates_provider_lifecycle_payload() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    unknown_recovery = cast(dict[str, object], provider_evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "provider-system-bot"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.unknown_recovery.operator_reviewed_by_must_be_human" in reason
    )
    assert "provider-system-bot" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_raw_identifier_redaction() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    created_order = cast(dict[str, object], provider_evidence["created_order"])
    created_order["provider_order_id_redacted"] = (
        "redacted:raw-provider-order-1234567890"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.created_order."
        "provider_order_id_redacted_must_use_allowed_redaction_format"
    ) in reason
    assert "raw-provider-order-1234567890" not in reason


def test_live_readiness_evidence_bundle_rejects_contact_like_provider_lifecycle_reviewer() -> None:
    bundle = _valid_bundle()
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    unknown_recovery = cast(dict[str, object], provider_evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "provider-admin@example.com"
    audit = cast(dict[str, object], provider_evidence["audit"])
    audit["reviewed_by"] = "https://ops.example.com/users/provider-admin"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.unknown_recovery."
        "operator_reviewed_by_must_be_logical_operator_id"
    ) in reason
    assert "provider_lifecycle_evidence.audit.reviewed_by_must_be_logical_operator_id" in reason
    assert "provider-admin@example.com" not in reason
    assert "ops.example.com" not in reason


def test_live_readiness_evidence_bundle_rejects_provider_lifecycle_summary_mismatch() -> None:
    bundle = _valid_bundle()
    provider = _external_check(bundle, "provider_lifecycle_evidence")
    provider["final_output"] = str(provider["final_output"]).replace(
        "status_observations=2",
        "status_observations=3",
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "provider_lifecycle_evidence.status_observations_must_match_final_output" in str(
        exc_info.value
    )


def test_live_readiness_evidence_bundle_cli_rejects_security_report_hash_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    report_path = tmp_path / "security-report.md"
    report_path.write_text("# Codex Security report\n\nNo findings.\n", encoding="utf-8")
    security_scan["report_path"] = report_path.name
    security_scan["report_sha256"] = "f" * 64
    evidence_path = tmp_path / "live_readiness_evidence_bundle.json"
    evidence_path.write_text(json.dumps(bundle), encoding="utf-8")

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL live_readiness_evidence_bundle" in output
    assert "security_scan.report_sha256_mismatch" in output


def test_live_readiness_evidence_bundle_rejects_reused_retained_evidence_reference() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_sha256"] = acceptance["evidence_sha256"]
    security_scan["report_uri"] = acceptance["evidence_uri"]

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.report_sha256_duplicates_retained_evidence_sha256" in reason
    assert "security_scan.report_uri_duplicates_retained_evidence_uri" in reason
    assert str(acceptance["evidence_sha256"]) not in reason
    assert str(acceptance["evidence_uri"]) not in reason


def test_live_readiness_evidence_bundle_rejects_canonical_retained_uri_reuse() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    artifacts = cast(list[dict[str, object]], provider_evidence["evidence_artifacts"])
    artifacts[0]["uri"] = (
        "https://EVIDENCE.KR-AUTOTRADING.NET:443/approvals/SCOPE-20260628-1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "system_order_scope_acceptance.evidence_uri_duplicates_retained_evidence_uri" in reason
    assert str(acceptance["evidence_uri"]) not in reason


def test_live_readiness_evidence_bundle_rejects_check_name_suffix_in_final_output() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["final_output"] = str(hosted["final_output"]).replace(
        "hosted_supabase_live_readiness",
        "hosted_supabase_live_readiness_bad",
    )
    live_alert = _local_check(bundle, "live_alert_drill")
    live_alert["final_output"] = str(live_alert["final_output"]).replace(
        "live_external_alert_drill",
        "live_external_alert_drill_bad",
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.hosted_supabase_live_readiness.final_output_missing_required_pass"
        in reason
    )
    assert "local_checks.live_alert_drill.final_output_missing_required_pass" in reason


def test_live_readiness_evidence_bundle_rejects_multiline_final_output() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["final_output"] = (
        f"{hosted['final_output']}\nFINAL=PASS hosted_supabase_live_readiness"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.hosted_supabase_live_readiness.final_output_must_be_single_line"
        in reason
    )


def test_live_readiness_evidence_bundle_rejects_hosted_skip() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["final_output"] = "FINAL=SKIP hosted_supabase_env_missing missing=SUPABASE_URL"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.hosted_supabase_live_readiness.final_output_missing_required_pass"
        in reason
    )
    assert (
        "external_checks.hosted_supabase_live_readiness.final_output_must_not_be_skip_or_fail"
        in reason
    )


def test_live_readiness_evidence_bundle_rejects_unscoped_hosted_readiness_output() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["final_output"] = "FINAL=PASS hosted_supabase_live_readiness"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.hosted_supabase_live_readiness."
        "final_output_missing_metrics="
        "anon_rpc_denied,anon_table_denied,authenticated_table_allowed,postgrest,realtime,"
        "service_rpc_allowed,service_table_allowed"
    ) in reason


def test_live_readiness_evidence_bundle_rejects_weak_hosted_readiness_counts() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["final_output"] = (
        "FINAL=PASS hosted_supabase_live_readiness postgrest=0 "
        "anon_rpc_denied=1 service_rpc_allowed=1 anon_table_denied=0 "
        "service_table_allowed=0 authenticated_table_allowed=1 realtime=0"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "external_checks.hosted_supabase_live_readiness.postgrest_must_be_1" in reason
    assert "external_checks.hosted_supabase_live_readiness.anon_rpc_denied_must_be_2" in reason
    assert "external_checks.hosted_supabase_live_readiness.service_rpc_allowed_must_be_2" in reason
    assert "external_checks.hosted_supabase_live_readiness.anon_table_denied_must_be_1" in reason
    assert (
        "external_checks.hosted_supabase_live_readiness."
        "service_table_allowed_must_be_1"
    ) in reason
    assert (
        "external_checks.hosted_supabase_live_readiness."
        "authenticated_table_allowed_must_be_2"
    ) in reason
    assert "external_checks.hosted_supabase_live_readiness.realtime_must_be_1" in reason


def test_live_readiness_evidence_bundle_rejects_hosted_readiness_extra_metrics() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["final_output"] = (
        "FINAL=PASS hosted_supabase_live_readiness postgrest=1 "
        "anon_rpc_denied=2 service_rpc_allowed=2 anon_table_denied=1 "
        "service_table_allowed=1 authenticated_table_allowed=2 realtime=1 "
        "project_url=https://secret.invalid realtime=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.hosted_supabase_live_readiness.final_output_unknown_metrics=project_url"
    ) in reason
    assert (
        "external_checks.hosted_supabase_live_readiness.final_output_duplicate_metrics=realtime"
    ) in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_unscoped_hosted_enable_output() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_live_enable_flow")
    hosted["final_output"] = "FINAL=PASS hosted_live_enable_flow"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.hosted_live_enable_flow.final_output_missing_metrics="
        "activation_consumed_once,request_created,requester_admin,review_accepted,"
        "reviewer_admin,second_activation_denied,self_review_denied"
    ) in reason


def test_live_readiness_evidence_bundle_rejects_weak_hosted_enable_counts() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_live_enable_flow")
    hosted["final_output"] = (
        "FINAL=PASS hosted_live_enable_flow requester_admin=1 reviewer_admin=0 "
        "request_created=1 self_review_denied=0 review_accepted=1 "
        "activation_consumed_once=0 second_activation_denied=0"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "external_checks.hosted_live_enable_flow.reviewer_admin_must_be_1" in reason
    assert "external_checks.hosted_live_enable_flow.self_review_denied_must_be_1" in reason
    assert "external_checks.hosted_live_enable_flow.activation_consumed_once_must_be_1" in reason
    assert "external_checks.hosted_live_enable_flow.second_activation_denied_must_be_1" in reason


def test_live_readiness_evidence_bundle_rejects_local_incident_ack_evidence() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident["surface"] = "local_mock"
    incident["final_output"] = (
        "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=1 acknowledged=false"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.surface_must_be_real_incident_channel"
        in reason
    )
    assert "external_checks.live_incident_response_drill.acknowledged_true_required" in reason
    assert "external_checks.live_incident_response_drill.ack_latency_ms_required" in reason


def test_live_readiness_evidence_bundle_requires_incident_channel_evidence() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    del incident["channel_evidence"]

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "external_checks.live_incident_response_drill.channel_evidence_must_be_object" in str(
        exc_info.value
    )


def test_live_readiness_evidence_bundle_rejects_slow_incident_delivery() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident["final_output"] = (
        "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=2001 "
        "acknowledged=true ack_latency_ms=2300 drill_id=incident-drill-20260628-1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "external_checks.live_incident_response_drill.max_latency_ms_above_2000" in str(
        exc_info.value
    )


def test_live_readiness_evidence_bundle_rejects_mock_incident_channel_evidence() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    evidence = cast(dict[str, object], incident["channel_evidence"])
    evidence["channel_name"] = "local_mock"
    evidence["evidence_uri"] = "https://alerts.example.test/mock-drill?token=abc"
    evidence["evidence_sha256"] = "not-a-sha"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "channel_name_must_not_be_mock_or_fixture"
    ) in reason
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "evidence_uri_must_not_be_mock_or_fixture"
    ) in reason
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "evidence_sha256_must_be_64_hex"
    ) in reason
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "evidence_uri_must_not_include_query_or_fragment"
    ) in reason
    assert "token=abc" not in reason


def test_live_readiness_evidence_bundle_rejects_url_like_incident_channel_name() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    evidence = cast(dict[str, object], incident["channel_evidence"])
    evidence["channel_name"] = "https://hooks.slack.com/services/T000/B000/SECRET"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "channel_name_must_be_logical_identifier"
    ) in reason
    assert "hooks.slack.com" not in reason
    assert "SECRET" not in reason


def test_live_readiness_evidence_bundle_rejects_automated_incident_ack() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    evidence = cast(dict[str, object], incident["channel_evidence"])
    evidence["operator_ack_by"] = "github-actions-bot"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "operator_ack_by_must_be_human"
    ) in reason
    assert "github-actions-bot" not in reason


def test_live_readiness_evidence_bundle_rejects_email_like_incident_ack_operator() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    evidence = cast(dict[str, object], incident["channel_evidence"])
    evidence["operator_ack_by"] = "ops-admin@example.com"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "operator_ack_by_must_be_logical_operator_id"
    ) in reason
    assert "ops-admin@example.com" not in reason


def test_live_readiness_evidence_bundle_rejects_incident_channel_drill_id_mismatch() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    evidence = cast(dict[str, object], incident["channel_evidence"])
    evidence["drill_id"] = "incident-drill-different"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "drill_id_must_match_incident_output"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_incident_final_output_extra_metrics() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident["final_output"] = (
        "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=17 "
        "acknowledged=true ack_latency_ms=2300 drill_id=incident-drill-20260628-1 "
        "webhook_url=https://secret.invalid delivered=4"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.final_output_unknown_metrics=webhook_url"
    ) in reason
    assert (
        "external_checks.live_incident_response_drill.final_output_duplicate_metrics=delivered"
    ) in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_incomplete_security_replay() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["independent_replay"] = False
    security_scan["threat_model_receipt"] = False
    security_scan["finding_discovery_receipt"] = False
    security_scan["reportable_findings"] = 1
    security_scan["completion_receipts"] = 47
    security_scan["validation_receipts"] = 2
    security_scan["attack_path_receipts"] = 1

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.independent_replay_must_be_true" in reason
    assert "security_scan.threat_model_receipt_must_be_true" in reason
    assert "security_scan.finding_discovery_receipt_must_be_true" in reason
    assert "security_scan.reportable_findings_must_be_0" in reason
    assert "security_scan.completion_receipts_must_equal_worklist_rows" in reason
    assert "security_scan.validation_receipts_must_equal_candidate_findings" in reason
    assert "security_scan.attack_path_receipts_must_equal_candidate_findings" in reason


def test_live_readiness_evidence_bundle_rejects_summary_only_security_scan() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["scan_profile"] = "summary_only"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "security_scan.scan_profile_must_be_security_diff_scan" in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_unbound_security_scan() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    del security_scan["source_head"]
    security_scan["source_diff_sha256"] = "not-a-sha"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.source_head_must_be_non_empty_string" in reason
    assert "security_scan.source_diff_sha256_must_be_64_hex" in reason


def test_live_readiness_evidence_bundle_rejects_weak_security_report_evidence() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_uri"] = "https://example.test/mock-security-report?token=abc"
    security_scan["report_sha256"] = "not-a-sha"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.report_uri_must_not_be_mock_fixture_or_local" in reason
    assert "security_scan.report_uri_must_not_include_query_or_fragment" in reason
    assert "security_scan.report_sha256_must_be_64_hex" in reason
    assert "token=abc" not in reason


def test_live_readiness_evidence_bundle_rejects_path_like_security_scan_id() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["scan_id"] = "C:security-scan-20260628"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.scan_id_must_be_logical_identifier" in reason
    assert "C:security-scan-20260628" not in reason


def test_live_readiness_evidence_bundle_rejects_absolute_security_report_path() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_path"] = "C:/Users/choey/.tmp/security/report.md"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.report_path_must_be_relative_retained_path" in reason
    assert "C:/Users/choey" not in reason


def test_live_readiness_evidence_bundle_rejects_escaping_security_report_path() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_path"] = "../outside/report.md"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.report_path_must_stay_under_evidence_dir" in reason
    assert "outside" not in reason


def test_live_readiness_evidence_bundle_rejects_non_https_security_report_uri() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_uri"] = "s3://ops-internal/security-scans/msp/report.md"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "security_scan.report_uri_must_be_https_uri" in reason
    assert "ops-internal/security-scans" not in reason


def test_live_readiness_evidence_bundle_rejects_malformed_retained_uri_ports() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident_channel = cast(dict[str, object], incident["channel_evidence"])
    incident_channel["evidence_uri"] = (
        "https://evidence.kr-autotrading.net:bad/incidents/live-incident-20260628.json"
    )
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["evidence_uri"] = (
        "https://evidence.kr-autotrading.net:99999/system-order-scope/acceptance.json"
    )
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_uri"] = (
        "https://security.kr-autotrading.net:0/security-scans/msp/report.md"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "evidence_uri_must_have_valid_port"
    ) in reason
    assert "system_order_scope_acceptance.evidence_uri_must_have_valid_port" in reason
    assert "security_scan.report_uri_must_have_valid_port" in reason
    assert "bad" not in reason
    assert "99999" not in reason
    assert ":0" not in reason


def test_live_readiness_evidence_bundle_rejects_retained_uri_path_traversal() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident_channel = cast(dict[str, object], incident["channel_evidence"])
    incident_channel["evidence_uri"] = (
        "https://evidence.kr-autotrading.net/incidents/%2e%2e/incident.json"
    )
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["evidence_uri"] = (
        "https://evidence.kr-autotrading.net/approvals/%2e%2e/scope.json"
    )
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_uri"] = (
        "https://evidence.kr-autotrading.net/security-scans/"
        "msp-20260628/%252e%252e/report.md"
    )
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    artifacts = cast(list[dict[str, object]], provider_evidence["evidence_artifacts"])
    artifacts[0]["uri"] = (
        "https://evidence.kr-autotrading.net/provider-lifecycle/"
        "toss-sandbox-2026-06-28/%2f/order-receipt"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "evidence_uri_must_not_include_path_traversal"
    ) in reason
    assert (
        "system_order_scope_acceptance.evidence_uri_must_not_include_path_traversal"
        in reason
    )
    assert "security_scan.report_uri_must_not_include_path_traversal" in reason
    assert (
        "provider_lifecycle_evidence.evidence_artifacts[0]."
        "uri_must_not_include_path_traversal"
    ) in reason
    assert "%2e%2e" not in reason
    assert "%252e%252e" not in reason
    assert "%2f" not in reason


def test_live_readiness_evidence_bundle_rejects_reused_retained_references() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident_channel = cast(dict[str, object], incident["channel_evidence"])
    incident_uri = str(incident_channel["evidence_uri"])
    incident_sha256 = str(incident_channel["evidence_sha256"])
    canonical_incident_uri = incident_uri.replace(
        "https://evidence.kr-autotrading.net",
        "https://EVIDENCE.KR-AUTOTRADING.NET:443",
    )

    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    artifacts = cast(list[dict[str, object]], provider_evidence["evidence_artifacts"])
    artifacts[0]["uri"] = canonical_incident_uri
    artifacts[0]["sha256"] = incident_sha256

    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["evidence_uri"] = canonical_incident_uri
    acceptance["evidence_sha256"] = incident_sha256

    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["report_sha256"] = incident_sha256

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.evidence_artifacts[0]."
        "sha256_duplicates_retained_evidence_sha256"
    ) in reason
    assert (
        "provider_lifecycle_evidence.evidence_artifacts[0]."
        "uri_duplicates_retained_evidence_uri"
    ) in reason
    assert (
        "system_order_scope_acceptance.evidence_sha256_"
        "duplicates_retained_evidence_sha256"
    ) in reason
    assert (
        "system_order_scope_acceptance.evidence_uri_duplicates_retained_evidence_uri"
        in reason
    )
    assert "security_scan.report_sha256_duplicates_retained_evidence_sha256" in reason
    assert "EVIDENCE.KR-AUTOTRADING.NET" not in reason
    assert incident_sha256 not in reason


def test_live_readiness_evidence_bundle_rejects_percent_encoded_reused_uri() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident_channel = cast(dict[str, object], incident["channel_evidence"])
    incident_channel["evidence_uri"] = (
        "https://evidence.kr-autotrading.net/incidents/%49NC-20260628-1"
    )
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["evidence_uri"] = (
        "https://EVIDENCE.KR-AUTOTRADING.NET:443/incidents/INC-20260628-1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "system_order_scope_acceptance.evidence_uri_duplicates_retained_evidence_uri"
        in reason
    )
    assert "%49NC" not in reason
    assert "EVIDENCE.KR-AUTOTRADING.NET" not in reason


def test_live_readiness_evidence_bundle_rejects_stale_check_output() -> None:
    bundle = _valid_bundle()
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["captured_at"] = "2026-06-28T00:59:59Z"

    with pytest.raises(BundleValidationError, match="captured_at_outside_bundle_window"):
        verify_live_readiness_evidence_bundle(bundle)


def test_live_readiness_evidence_bundle_rejects_overlong_evidence_window() -> None:
    bundle = _valid_bundle()
    bundle["reviewed_at"] = "2026-06-29T01:20:01Z"

    with pytest.raises(BundleValidationError, match="evidence_window_must_not_exceed_24h"):
        verify_live_readiness_evidence_bundle(bundle)


def test_live_readiness_evidence_bundle_rejects_reviewed_at_equal_generated_at() -> None:
    bundle = _valid_bundle()
    bundle["generated_at"] = "2026-06-28T01:20:00Z"
    bundle["reviewed_at"] = "2026-06-28T01:20:00Z"

    with pytest.raises(
        BundleValidationError,
        match="reviewed_at_must_be_after_generated_at",
    ):
        verify_live_readiness_evidence_bundle(bundle)


def test_live_readiness_evidence_bundle_rejects_missing_scope_acceptance() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["accepted"] = False

    with pytest.raises(BundleValidationError, match="accepted_must_be_true"):
        verify_live_readiness_evidence_bundle(bundle)


def test_live_readiness_evidence_bundle_rejects_weak_scope_acceptance_evidence() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["evidence_uri"] = "https://example.test/mock-scope-acceptance?token=abc"
    acceptance["evidence_sha256"] = "not-a-sha"
    acceptance["runtime_env_value_confirmed"] = False

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "system_order_scope_acceptance.evidence_uri_must_not_be_mock_or_fixture" in reason
    assert "system_order_scope_acceptance.evidence_uri_must_not_include_query_or_fragment" in reason
    assert "system_order_scope_acceptance.evidence_sha256_must_be_64_hex" in reason
    assert "system_order_scope_acceptance.runtime_env_value_confirmed_must_be_true" in reason
    assert "token=abc" not in reason


def test_live_readiness_evidence_bundle_rejects_non_https_scope_evidence_uri() -> None:
    bundle = _valid_bundle()
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    acceptance["evidence_uri"] = "ops://approvals/SCOPE-20260628-1"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "system_order_scope_acceptance.evidence_uri_must_be_https_uri" in reason
    assert "approvals/SCOPE-20260628-1" not in reason


def test_live_readiness_evidence_bundle_rejects_sensitive_keys_without_leaking_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = _valid_bundle()
    bundle["operator_jwt"] = "secret-jwt-that-must-not-print"
    evidence_path = _write_evidence(tmp_path, bundle)

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL live_readiness_evidence_bundle" in output
    assert "sensitive_key_not_allowed:bundle.operator_jwt" in output
    assert "secret-jwt-that-must-not-print" not in output


def test_live_readiness_evidence_bundle_rejects_sensitive_scope_key_without_leak() -> None:
    bundle = _valid_bundle()
    scope_acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    scope_acceptance["operator_jwt"] = "scope-secret-that-must-not-print"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "sensitive_key_not_allowed:bundle.system_order_scope_acceptance.operator_jwt"
    ) in reason
    assert "system_order_scope_acceptance.unknown_keys=operator_jwt" in reason
    assert "scope-secret-that-must-not-print" not in reason


def test_live_readiness_evidence_bundle_rejects_sensitive_security_key_without_leak() -> None:
    bundle = _valid_bundle()
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["operator_token"] = "security-secret-that-must-not-print"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "sensitive_key_not_allowed:bundle.security_scan.operator_token" in reason
    assert "security_scan.unknown_keys=operator_token" in reason
    assert "security-secret-that-must-not-print" not in reason


def test_live_readiness_evidence_bundle_rejects_sensitive_incident_key_without_leak() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident_channel = cast(dict[str, object], incident["channel_evidence"])
    incident_channel["webhook_token"] = "incident-secret-that-must-not-print"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "sensitive_key_not_allowed:"
        "bundle.external_checks.live_incident_response_drill.channel_evidence.webhook_token"
    ) in reason
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "unknown_keys=webhook_token"
    ) in reason
    assert "incident-secret-that-must-not-print" not in reason


def test_live_readiness_evidence_bundle_rejects_unknown_root_and_check_keys() -> None:
    bundle = _valid_bundle()
    bundle["operator_note"] = "must stay outside release evidence"
    hosted = _external_check(bundle, "hosted_supabase_live_readiness")
    hosted["raw_output"] = "unbounded logs must not be embedded"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "bundle.unknown_keys=operator_note" in reason
    assert "external_checks.hosted_supabase_live_readiness.unknown_keys=raw_output" in reason
    assert "unbounded logs must not be embedded" not in reason


def test_live_readiness_evidence_bundle_rejects_unknown_nested_evidence_keys() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident_channel = cast(dict[str, object], incident["channel_evidence"])
    incident_channel["raw_ack_payload"] = {"body": "must not be embedded"}
    scope_acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    scope_acceptance["approval_screenshot"] = "must be retained by URI and SHA only"
    security_scan = cast(dict[str, object], bundle["security_scan"])
    security_scan["raw_report"] = "must stay in the retained report artifact"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence.unknown_keys=raw_ack_payload"
    ) in reason
    assert "system_order_scope_acceptance.unknown_keys=approval_screenshot" in reason
    assert "security_scan.unknown_keys=raw_report" in reason
    assert "must not be embedded" not in reason


def test_live_readiness_evidence_bundle_rejects_incident_capture_not_after_ack() -> None:
    bundle = _valid_bundle()
    incident = _external_check(bundle, "live_incident_response_drill")
    incident_channel = cast(dict[str, object], incident["channel_evidence"])
    incident_channel["captured_at"] = "2026-06-28T01:04:12Z"
    incident_channel["operator_ack_at"] = "2026-06-28T01:04:12Z"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.live_incident_response_drill.channel_evidence."
        "captured_at_must_be_after_operator_ack_at"
    ) in reason
    assert "2026-06-28T01" not in reason


def test_live_readiness_evidence_bundle_rejects_unscoped_provider_lifecycle_output() -> None:
    bundle = _valid_bundle()
    provider_lifecycle = _external_check(bundle, "provider_lifecycle_evidence")
    provider_lifecycle["final_output"] = (
        "FINAL=PASS provider_lifecycle_evidence provider=toss environment=sandbox"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.provider_lifecycle_evidence.final_output_missing_metrics="
        "audit_logs_reviewed,evidence_artifacts,status_observations"
    ) in reason


def test_live_readiness_evidence_bundle_rejects_weak_provider_lifecycle_counts() -> None:
    bundle = _valid_bundle()
    provider_lifecycle = _external_check(bundle, "provider_lifecycle_evidence")
    provider_lifecycle["final_output"] = (
        "FINAL=PASS provider_lifecycle_evidence provider=toss environment=sandbox "
        "status_observations=1 audit_logs_reviewed=1 evidence_artifacts=4"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "external_checks.provider_lifecycle_evidence.status_observations_below_2" in reason
    assert "external_checks.provider_lifecycle_evidence.audit_logs_reviewed_below_2" in reason
    assert "external_checks.provider_lifecycle_evidence.evidence_artifacts_must_be_5" in reason


def test_live_readiness_evidence_bundle_rejects_provider_lifecycle_extra_metrics() -> None:
    bundle = _valid_bundle()
    provider_lifecycle = _external_check(bundle, "provider_lifecycle_evidence")
    provider_lifecycle["final_output"] = (
        "FINAL=PASS provider_lifecycle_evidence provider=toss environment=sandbox "
        "status_observations=2 audit_logs_reviewed=2 evidence_artifacts=5 "
        "raw_url=https://secret.invalid status_observations=2"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "external_checks.provider_lifecycle_evidence.final_output_unknown_metrics=raw_url"
    ) in reason
    assert (
        "external_checks.provider_lifecycle_evidence."
        "final_output_duplicate_metrics=status_observations"
    ) in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_unscoped_provider_gap_output() -> None:
    bundle = _valid_bundle()
    local_checks = cast(dict[str, object], bundle["local_checks"])
    provider_gap = cast(dict[str, object], local_checks["provider_contract_gaps"])
    provider_gap["final_output"] = "FINAL=PASS"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "local_checks.provider_contract_gaps.final_output_missing_required_pass" in reason
    assert "local_checks.provider_contract_gaps.final_output_missing_metrics=" in reason
    for metric in (
        "blocking_unknown_gaps",
        "invalid_status_gaps",
        "total_gaps",
        "system_order_scope_accepted",
        "provider_gap_evidence",
        "warning_partial_gap_ids",
        "warning_partial_gaps",
    ):
        assert metric in reason


def test_live_readiness_evidence_bundle_rejects_provider_gap_unknown_count() -> None:
    bundle = _valid_bundle()
    local_checks = cast(dict[str, object], bundle["local_checks"])
    provider_gap = cast(dict[str, object], local_checks["provider_contract_gaps"])
    provider_gap["final_output"] = (
        "FINAL=PASS provider_contract_gaps total_gaps=18 "
        "blocking_unknown_gaps=1 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:"
        "partial-system-only-accepted-fail-closed system_order_scope_accepted=1 "
        "provider_gap_evidence=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "local_checks.provider_contract_gaps.blocking_unknown_gaps_must_be_0" in str(
        exc_info.value
    )


def test_live_readiness_evidence_bundle_rejects_provider_gap_invalid_status_count() -> None:
    bundle = _valid_bundle()
    local_checks = cast(dict[str, object], bundle["local_checks"])
    provider_gap = cast(dict[str, object], local_checks["provider_contract_gaps"])
    provider_gap["final_output"] = (
        "FINAL=PASS provider_contract_gaps total_gaps=18 "
        "blocking_unknown_gaps=0 invalid_status_gaps=1 warning_partial_gaps=1 "
        "warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:"
        "partial-system-only-accepted-fail-closed system_order_scope_accepted=1 "
        "provider_gap_evidence=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "local_checks.provider_contract_gaps.invalid_status_gaps_must_be_0" in str(
        exc_info.value
    )


def test_live_readiness_evidence_bundle_rejects_extra_provider_gap_warnings() -> None:
    bundle = _valid_bundle()
    local_checks = cast(dict[str, object], bundle["local_checks"])
    provider_gap = cast(dict[str, object], local_checks["provider_contract_gaps"])
    provider_gap["final_output"] = (
        "FINAL=PASS provider_contract_gaps total_gaps=18 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=2 "
        "warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:"
        "partial-system-only-accepted-fail-closed,"
        "opendart:account-name-mapping:verified-partial-fundamentals-implemented "
        "system_order_scope_accepted=1 provider_gap_evidence=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "local_checks.provider_contract_gaps.warning_partial_gaps_above_1" in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_wrong_provider_gap_warning_id() -> None:
    bundle = _valid_bundle()
    local_checks = cast(dict[str, object], bundle["local_checks"])
    provider_gap = cast(dict[str, object], local_checks["provider_contract_gaps"])
    provider_gap["final_output"] = (
        "FINAL=PASS provider_contract_gaps total_gaps=18 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=opendart:account-name-mapping:"
        "verified-partial-fundamentals-implemented system_order_scope_accepted=1 "
        "provider_gap_evidence=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "local_checks.provider_contract_gaps."
        "warning_partial_gap_ids_must_match_documented_toss_scope_limitation"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_provider_gap_warning_id_count_mismatch() -> None:
    bundle = _valid_bundle()
    local_checks = cast(dict[str, object], bundle["local_checks"])
    provider_gap = cast(dict[str, object], local_checks["provider_contract_gaps"])
    provider_gap["final_output"] = (
        "FINAL=PASS provider_contract_gaps total_gaps=18 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=none system_order_scope_accepted=1 provider_gap_evidence=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "local_checks.provider_contract_gaps."
        "warning_partial_gap_ids_count_must_match_warning_count"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_gap_warning_without_scope_metric() -> None:
    bundle = _valid_bundle()
    provider_gap = _local_check(bundle, "provider_contract_gaps")
    provider_gap["final_output"] = (
        "FINAL=PASS provider_contract_gaps total_gaps=18 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:"
        "partial-system-only-accepted-fail-closed system_order_scope_accepted=0 "
        "provider_gap_evidence=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "local_checks.provider_contract_gaps."
        "system_order_scope_accepted_required_for_warnings"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_missing_provider_gap_evidence_payload() -> None:
    bundle = _valid_bundle()
    del bundle["provider_gap_evidence"]

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "bundle.provider_gap_evidence_must_be_object" in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_provider_gap_evidence_mismatch() -> None:
    bundle = _valid_bundle()
    provider_gap_evidence = cast(dict[str, object], bundle["provider_gap_evidence"])
    provider_gap_evidence["api_gaps_sha256"] = "0" * 64

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert "provider_gap_evidence.api_gaps_sha256_must_match_api_gaps" in str(
        exc_info.value
    )


def test_live_readiness_evidence_bundle_rejects_provider_gap_output_without_evidence() -> None:
    bundle = _valid_bundle()
    provider_gap = _local_check(bundle, "provider_contract_gaps")
    provider_gap["final_output"] = (
        "FINAL=PASS provider_contract_gaps total_gaps=18 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:"
        "partial-system-only-accepted-fail-closed system_order_scope_accepted=1 "
        "provider_gap_evidence=0"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "local_checks.provider_contract_gaps.provider_gap_evidence_must_be_1" in reason
    assert "provider_gap_evidence.final_output_must_confirm_evidence" in reason


def test_live_readiness_evidence_bundle_rejects_mutated_live_enable_output() -> None:
    bundle = _valid_bundle()
    live_enable = _local_check(bundle, "live_enable_migration")
    live_enable["final_output"] = (
        "FINAL=PASS live_enable_consumed_once rpc_hardening raw_url=https://secret.invalid"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "local_checks.live_enable_migration.final_output_must_match_live_enable_migration_pass"
    ) in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_unscoped_live_execution_safety_output() -> None:
    bundle = _valid_bundle()
    safety = _local_check(bundle, "live_execution_safety_drill")
    safety["final_output"] = "FINAL=PASS live_execution_safety_drill"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "local_checks.live_execution_safety_drill.final_output_missing_metrics="
        "broker_calls,duplicate_blocked,missing_evidence_blocked,"
        "pre_broker_manual_check,provider_result_recorded"
    ) in reason


def test_live_readiness_evidence_bundle_rejects_weak_live_execution_safety_counts() -> None:
    bundle = _valid_bundle()
    safety = _local_check(bundle, "live_execution_safety_drill")
    safety["final_output"] = (
        "FINAL=PASS live_execution_safety_drill missing_evidence_blocked=0 "
        "pre_broker_manual_check=0 provider_result_recorded=0 duplicate_blocked=0 "
        "broker_calls=2"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "local_checks.live_execution_safety_drill."
        "missing_evidence_blocked_must_be_1"
    ) in reason
    assert (
        "local_checks.live_execution_safety_drill."
        "pre_broker_manual_check_must_be_1"
    ) in reason
    assert (
        "local_checks.live_execution_safety_drill."
        "provider_result_recorded_must_be_1"
    ) in reason
    assert "local_checks.live_execution_safety_drill.duplicate_blocked_must_be_1" in reason
    assert "local_checks.live_execution_safety_drill.broker_calls_must_be_1" in reason


def test_live_readiness_evidence_bundle_rejects_live_execution_safety_extra_metrics() -> None:
    bundle = _valid_bundle()
    safety = _local_check(bundle, "live_execution_safety_drill")
    safety["final_output"] = (
        "FINAL=PASS live_execution_safety_drill missing_evidence_blocked=1 "
        "pre_broker_manual_check=1 provider_result_recorded=1 duplicate_blocked=1 "
        "broker_calls=1 raw_url=https://secret.invalid broker_calls=1"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "local_checks.live_execution_safety_drill.final_output_unknown_metrics=raw_url"
        in reason
    )
    assert (
        "local_checks.live_execution_safety_drill."
        "final_output_duplicate_metrics=broker_calls"
    ) in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_unscoped_live_recovery_output() -> None:
    bundle = _valid_bundle()
    recovery = _local_check(bundle, "live_recovery_drill")
    recovery["final_output"] = "FINAL=PASS live_recovery_drill"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "local_checks.live_recovery_drill.final_output_missing_metrics="
        "cancel_calls,cancel_confirmed,cancel_unknown,manual_check_events,"
        "manual_check_preserved,pending_order_blocked,reconciled_updates,status_calls"
    ) in reason


def test_live_readiness_evidence_bundle_rejects_weak_live_recovery_counts() -> None:
    bundle = _valid_bundle()
    recovery = _local_check(bundle, "live_recovery_drill")
    recovery["final_output"] = (
        "FINAL=PASS live_recovery_drill reconciled_updates=0 "
        "manual_check_events=1 cancel_confirmed=0 cancel_unknown=0 "
        "status_calls=3 cancel_calls=1 pending_order_blocked=0 manual_check_preserved=0"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "local_checks.live_recovery_drill.reconciled_updates_must_be_1" in reason
    assert "local_checks.live_recovery_drill.manual_check_events_must_be_2" in reason
    assert "local_checks.live_recovery_drill.status_calls_must_be_4" in reason
    assert "local_checks.live_recovery_drill.cancel_calls_must_be_2" in reason
    assert "local_checks.live_recovery_drill.pending_order_blocked_must_be_1" in reason
    assert "local_checks.live_recovery_drill.manual_check_preserved_must_be_1" in reason


def test_live_readiness_evidence_bundle_rejects_live_recovery_extra_metrics() -> None:
    bundle = _valid_bundle()
    recovery = _local_check(bundle, "live_recovery_drill")
    recovery["final_output"] = (
        "FINAL=PASS live_recovery_drill reconciled_updates=1 "
        "manual_check_events=2 cancel_confirmed=1 cancel_unknown=1 "
        "status_calls=4 cancel_calls=2 pending_order_blocked=1 manual_check_preserved=1 "
        "raw_url=https://secret.invalid "
        "status_calls=4"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "local_checks.live_recovery_drill.final_output_unknown_metrics=raw_url" in reason
    assert "local_checks.live_recovery_drill.final_output_duplicate_metrics=status_calls" in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_unscoped_live_alert_output() -> None:
    bundle = _valid_bundle()
    alert = _local_check(bundle, "live_alert_drill")
    alert["final_output"] = "FINAL=PASS live_external_alert_drill"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    assert (
        "local_checks.live_alert_drill.final_output_missing_metrics=delivered,max_latency_ms"
    ) in str(exc_info.value)


def test_live_readiness_evidence_bundle_rejects_weak_live_alert_counts() -> None:
    bundle = _valid_bundle()
    alert = _local_check(bundle, "live_alert_drill")
    alert["final_output"] = "FINAL=PASS live_external_alert_drill delivered=3 max_latency_ms=2001"

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "local_checks.live_alert_drill.delivered_must_be_4" in reason
    assert "local_checks.live_alert_drill.max_latency_ms_above_2000" in reason


def test_live_readiness_evidence_bundle_rejects_live_alert_extra_metrics() -> None:
    bundle = _valid_bundle()
    alert = _local_check(bundle, "live_alert_drill")
    alert["final_output"] = (
        "FINAL=PASS live_external_alert_drill delivered=4 max_latency_ms=17 "
        "raw_url=https://secret.invalid delivered=4"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "local_checks.live_alert_drill.final_output_unknown_metrics=raw_url" in reason
    assert "local_checks.live_alert_drill.final_output_duplicate_metrics=delivered" in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_weak_scorecard_output() -> None:
    bundle = _valid_bundle()
    scorecard = _local_check(bundle, "live_readiness_scorecard")
    scorecard["final_output"] = (
        "FINAL=PASS live_readiness_scorecard scorecard_security_scan=0 "
        "worklist_rows=0 candidate_findings=0 reportable_findings=1 "
        "raw_url=https://secret.invalid"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert "local_checks.live_readiness_scorecard.scorecard_security_scan_must_be_1" in reason
    assert "local_checks.live_readiness_scorecard.reportable_findings_must_be_0" in reason
    assert "local_checks.live_readiness_scorecard.worklist_rows_must_be_positive" in reason
    assert "local_checks.live_readiness_scorecard.final_output_unknown_metrics=raw_url" in reason
    assert "secret.invalid" not in reason


def test_live_readiness_evidence_bundle_rejects_stale_worker_release_output() -> None:
    bundle = _valid_bundle()
    freshness = _local_check(bundle, "worker_release_freshness")
    freshness["final_output"] = (
        "FINAL=PASS worker_release_freshness "
        "expected_sha_short=2bac8362b504 observed_sha_short=976ced927831 "
        "heartbeat_age_sec=301 max_age_sec=300 raw_url=https://secret.invalid"
    )

    with pytest.raises(BundleValidationError) as exc_info:
        verify_live_readiness_evidence_bundle(bundle)

    reason = str(exc_info.value)
    assert (
        "local_checks.worker_release_freshness.observed_sha_short_must_match_expected"
        in reason
    )
    assert (
        "local_checks.worker_release_freshness.heartbeat_age_sec_above_max_age_sec"
        in reason
    )
    assert (
        "local_checks.worker_release_freshness.final_output_unknown_metrics=raw_url"
        in reason
    )
    assert "secret.invalid" not in reason


def _external_check(bundle: dict[str, object], name: str) -> dict[str, object]:
    external_checks = cast(dict[str, object], bundle["external_checks"])
    return cast(dict[str, object], external_checks[name])


def _local_check(bundle: dict[str, object], name: str) -> dict[str, object]:
    local_checks = cast(dict[str, object], bundle["local_checks"])
    return cast(dict[str, object], local_checks[name])


def _write_evidence(tmp_path: Path, evidence: dict[str, object]) -> Path:
    _bind_security_report_file(tmp_path, evidence)
    path = tmp_path / "live_readiness_evidence_bundle.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _bind_security_report_file(tmp_path: Path, evidence: dict[str, object]) -> None:
    security_scan = evidence.get("security_scan")
    if not isinstance(security_scan, dict):
        return
    report_path = tmp_path / "security-report.md"
    report_path.write_text("# Codex Security report\n\nNo findings.\n", encoding="utf-8")
    security_scan["report_path"] = report_path.name
    security_scan["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()


def _valid_bundle() -> dict[str, object]:
    return copy.deepcopy(
        {
            "schema_version": 1,
            "environment": "staging",
            "generated_at": "2026-06-28T01:00:00Z",
            "reviewed_at": "2026-06-28T01:20:00Z",
            "reviewed_by": "release-admin-1",
            "external_checks": {
                "hosted_supabase_live_readiness": {
                    "surface": "hosted_supabase",
                    "captured_at": "2026-06-28T01:01:00Z",
                    "final_output": (
                        "FINAL=PASS hosted_supabase_live_readiness postgrest=1 "
                        "anon_rpc_denied=2 service_rpc_allowed=2 "
                        "anon_table_denied=1 service_table_allowed=1 "
                        "authenticated_table_allowed=2 realtime=1"
                    ),
                },
                "hosted_live_enable_flow": {
                    "surface": "hosted_supabase",
                    "captured_at": "2026-06-28T01:02:00Z",
                    "final_output": (
                        "FINAL=PASS hosted_live_enable_flow requester_admin=1 reviewer_admin=1 "
                        "request_created=1 self_review_denied=1 review_accepted=1 "
                        "activation_consumed_once=1 second_activation_denied=1"
                    ),
                },
                "provider_lifecycle_evidence": {
                    "surface": "toss_sandbox_or_live",
                    "captured_at": "2026-06-28T01:03:00Z",
                    "final_output": (
                        "FINAL=PASS provider_lifecycle_evidence provider=toss "
                        "environment=sandbox status_observations=2 "
                        "audit_logs_reviewed=2 evidence_artifacts=5"
                    ),
                },
                "live_incident_response_drill": {
                    "surface": "real_incident_channel",
                    "captured_at": "2026-06-28T01:04:00Z",
                    "final_output": (
                        "FINAL=PASS live_incident_response_drill delivered=4 "
                        "max_latency_ms=17 acknowledged=true ack_latency_ms=2300 "
                        "drill_id=incident-drill-20260628-1"
                    ),
                    "channel_evidence": {
                        "captured_at": "2026-06-28T01:04:20Z",
                        "channel_name": "ops-live-incidents",
                        "drill_id": "incident-drill-20260628-1",
                        "evidence_uri": "https://evidence.kr-autotrading.net/incidents/INC-20260628-1",
                        "evidence_sha256": (
                            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                        ),
                        "operator_ack": True,
                        "operator_ack_at": "2026-06-28T01:04:12Z",
                        "operator_ack_by": "ops-admin-2",
                    },
                },
            },
            "local_checks": {
                "worker_release_freshness": {
                    "captured_at": "2026-06-28T01:04:30Z",
                    "final_output": (
                        "FINAL=PASS worker_release_freshness "
                        "expected_sha_short=2bac8362b504 "
                        "observed_sha_short=2bac8362b504 "
                        "heartbeat_age_sec=12 max_age_sec=300"
                    ),
                },
                "live_enable_migration": {
                    "captured_at": "2026-06-28T01:05:00Z",
                    "final_output": "FINAL=PASS live_enable_consumed_once rpc_hardening",
                },
                "live_execution_safety_drill": {
                    "captured_at": "2026-06-28T01:05:30Z",
                    "final_output": (
                        "FINAL=PASS live_execution_safety_drill "
                        "missing_evidence_blocked=1 pre_broker_manual_check=1 "
                        "provider_result_recorded=1 duplicate_blocked=1 broker_calls=1"
                    ),
                },
                "live_recovery_drill": {
                    "captured_at": "2026-06-28T01:06:00Z",
                    "final_output": (
                        "FINAL=PASS live_recovery_drill reconciled_updates=1 "
                        "manual_check_events=2 cancel_confirmed=1 cancel_unknown=1 "
                        "status_calls=4 cancel_calls=2 pending_order_blocked=1 "
                        "manual_check_preserved=1"
                    ),
                },
                "live_alert_drill": {
                    "captured_at": "2026-06-28T01:07:00Z",
                    "final_output": (
                        "FINAL=PASS live_external_alert_drill delivered=4 max_latency_ms=17"
                    ),
                },
                "provider_contract_gaps": {
                    "captured_at": "2026-06-28T01:08:00Z",
                    "final_output": (
                        "FINAL=PASS provider_contract_gaps total_gaps=18 "
                        "blocking_unknown_gaps=0 invalid_status_gaps=0 "
                        "warning_partial_gaps=1 "
                        "warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:"
                        "partial-system-only-accepted-fail-closed "
                        "system_order_scope_accepted=1 provider_gap_evidence=1"
                    ),
                },
                "live_readiness_scorecard": {
                    "captured_at": "2026-06-28T01:08:30Z",
                    "final_output": (
                        "FINAL=PASS live_readiness_scorecard scorecard_security_scan=1 "
                        "worklist_rows=46 candidate_findings=3 reportable_findings=0"
                    ),
                },
            },
            "provider_lifecycle_evidence": _valid_provider_lifecycle_evidence(),
            "provider_gap_evidence": _valid_provider_gap_evidence(),
            "system_order_scope_acceptance": {
                "accepted": True,
                "scope": "system_created_live_orders_only",
                "broker": "toss",
                "limitation": "broker_wide_closed_order_history_unavailable",
                "runtime_env_var": "LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED",
                "runtime_env_value_confirmed": True,
                "deployment_environment": "staging",
                "accepted_by": "ops-admin-1",
                "accepted_at": "2026-06-28T01:09:00Z",
                "evidence_captured_at": "2026-06-28T01:10:00Z",
                "evidence_uri": "https://evidence.kr-autotrading.net/approvals/SCOPE-20260628-1",
                "evidence_sha256": (
                    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
                ),
            },
            "security_scan": {
                "scan_id": "msp-20260628-independent-replay",
                "report_path": "security-report.md",
                "report_uri": "https://evidence.kr-autotrading.net/security-scans/msp-20260628/report.md",
                "report_sha256": (
                    "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
                ),
                "source_head": "a" * 40,
                "source_diff_sha256": "b" * 64,
                "completed_at": "2026-06-28T01:10:00Z",
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
            },
        }
    )


def _valid_provider_lifecycle_evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "drill_id": "toss-sandbox-2026-06-28",
        "provider": "toss",
        "environment": "sandbox",
        "started_at": "2026-06-28T01:00:00Z",
        "completed_at": "2026-06-28T01:10:00Z",
        "live_order_allowed_before": False,
        "live_order_allowed_after": False,
        "created_order": {
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "created_at": "2026-06-28T01:00:30Z",
            "symbol": "005930",
            "action": "buy",
            "order_type": "KRX_LIMIT",
            "provider_order_id_redacted": "toss_order_...abcd",
            "amount_krw": 75000,
            "status_after_create": "sent",
        },
        "provider_status_sequence": [
            {
                "observed_at": "2026-06-28T01:01:00Z",
                "local_order_id": "11111111-1111-4111-8111-111111111111",
                "provider_status": "PENDING",
                "local_status": "sent",
            },
            {
                "observed_at": "2026-06-28T01:04:30Z",
                "local_order_id": "11111111-1111-4111-8111-111111111111",
                "provider_status": "CANCELED",
                "local_status": "canceled",
            },
        ],
        "cancel_probe": {
            "attempted": True,
            "attempted_at": "2026-06-28T01:04:00Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_cancel_id_redacted": "toss_cancel_...dcba",
            "provider_final_status": "CANCELED",
            "local_status": "canceled",
        },
        "unknown_recovery": {
            "order_id": "11111111-1111-4111-8111-111111111111",
            "reason": "provider_timeout",
            "engine_event_message": "live_order_manual_check_still_unknown",
            "operator_reviewed_at": "2026-06-28T01:07:00Z",
            "operator_reviewed_by": "provider-admin-1",
            "final_status": "canceled",
        },
        "audit": {
            "orders_reviewed": 3,
            "engine_events_reviewed": 2,
            "audit_logs_reviewed": 2,
            "reviewed_by": "provider-admin-2",
            "reviewed_at": "2026-06-28T01:09:00Z",
        },
        "evidence_artifacts": [
            {
                "type": "broker_order_receipt",
                "drill_id": "toss-sandbox-2026-06-28",
                "uri": "https://evidence.kr-autotrading.net/provider-lifecycle/toss-sandbox-2026-06-28/order",
                "sha256": _sha256("a"),
                "captured_at": "2026-06-28T01:02:00Z",
            },
            {
                "type": "provider_status_export",
                "drill_id": "toss-sandbox-2026-06-28",
                "uri": "https://evidence.kr-autotrading.net/provider-lifecycle/toss-sandbox-2026-06-28/status",
                "sha256": _sha256("b"),
                "captured_at": "2026-06-28T01:05:00Z",
            },
            {
                "type": "cancel_confirmation",
                "drill_id": "toss-sandbox-2026-06-28",
                "uri": "https://evidence.kr-autotrading.net/provider-lifecycle/toss-sandbox-2026-06-28/cancel",
                "sha256": _sha256("c"),
                "captured_at": "2026-06-28T01:05:00Z",
            },
            {
                "type": "unknown_recovery_review",
                "drill_id": "toss-sandbox-2026-06-28",
                "uri": "https://evidence.kr-autotrading.net/provider-lifecycle/toss-sandbox-2026-06-28/unknown",
                "sha256": _sha256("d"),
                "captured_at": "2026-06-28T01:07:30Z",
            },
            {
                "type": "repository_audit_export",
                "drill_id": "toss-sandbox-2026-06-28",
                "uri": "https://evidence.kr-autotrading.net/provider-lifecycle/toss-sandbox-2026-06-28/audit",
                "sha256": _sha256("e"),
                "captured_at": "2026-06-28T01:09:30Z",
            },
        ],
    }


def _valid_provider_gap_evidence() -> dict[str, object]:
    api_gaps_path = Path(__file__).resolve().parents[5] / "docs" / "API_GAPS.md"
    api_gaps_markdown = api_gaps_path.read_text(encoding="utf-8")
    gaps = evaluate_provider_api_gaps(api_gaps_markdown).gaps
    gap_ids = [provider_api_gap_id(gap) for gap in gaps]
    provider_gap_ids: dict[str, list[str]] = {}
    for gap in gaps:
        provider_gap_ids.setdefault(gap.provider, []).append(provider_api_gap_id(gap))
    return {
        "schema_version": 1,
        "api_gaps_sha256": hashlib.sha256(
            api_gaps_markdown.encode("utf-8")
        ).hexdigest(),
        "gap_ids": gap_ids,
        "captured_at": "2026-06-28T01:02:30Z",
        "source_artifacts": [
            {
                "provider": provider,
                "source_name": f"{_slug(provider)}-source-artifact",
                "gap_ids": ids,
                "artifact_uri": (
                    "https://evidence.kr-autotrading.net/provider-gaps/"
                    f"{_slug(provider)}-source.json"
                ),
                "artifact_sha256": hashlib.sha256(
                    f"provider-gap-{provider}".encode()
                ).hexdigest(),
                "captured_at": "2026-06-28T01:02:40Z",
            }
            for provider, ids in sorted(provider_gap_ids.items())
        ],
    }


def _sha256(prefix: str) -> str:
    return (prefix * 64)[:64]


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


def _real_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _attach_provider_remote_artifact_hashes(
    bundle: dict[str, object],
) -> dict[str, bytes]:
    bodies: dict[str, bytes] = {}
    provider_evidence = cast(dict[str, object], bundle["provider_lifecycle_evidence"])
    artifacts = cast(list[dict[str, object]], provider_evidence["evidence_artifacts"])
    for index, artifact in enumerate(artifacts):
        uri = cast(str, artifact["uri"])
        body = f"provider-bundle-artifact-{index}".encode()
        artifact["sha256"] = _real_sha256(body)
        bodies[uri] = body
    return bodies


def _attach_incident_remote_evidence_hash(bundle: dict[str, object]) -> dict[str, bytes]:
    incident = _external_check(bundle, "live_incident_response_drill")
    channel_evidence = cast(dict[str, object], incident["channel_evidence"])
    uri = cast(str, channel_evidence["evidence_uri"])
    body = b"incident-bundle-evidence"
    channel_evidence["evidence_sha256"] = _real_sha256(body)
    return {uri: body}


def _attach_system_scope_remote_evidence_hash(
    bundle: dict[str, object],
) -> dict[str, bytes]:
    acceptance = cast(dict[str, object], bundle["system_order_scope_acceptance"])
    uri = cast(str, acceptance["evidence_uri"])
    body = b"system-scope-bundle-evidence"
    acceptance["evidence_sha256"] = _real_sha256(body)
    return {uri: body}

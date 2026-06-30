from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from app.application.services.provider_gap_gate import (
    evaluate_provider_api_gaps,
    provider_api_gap_id,
)
from app.tools.collect_live_readiness_evidence_bundle import (
    CollectorConfig,
    CollectorError,
    CommandResult,
    CommandSpec,
    _collect_git_evidence,
    collect_live_readiness_evidence_bundle,
)


def test_live_readiness_evidence_collector_writes_valid_bundle(tmp_path: Path) -> None:
    output_path = tmp_path / "bundle.json"
    config = _config(tmp_path, output_path=output_path)

    bundle, summary = collect_live_readiness_evidence_bundle(
        config,
        command_runner=_pass_runner,
        git_evidence_reader=_git_evidence,
        clock=_clock(),
    )

    assert summary.external_checks == 4
    assert summary.local_checks == 7
    assert summary.security_scan is True
    assert summary.system_order_scope_accepted is True
    assert output_path.exists()
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written == bundle
    assert written["external_checks"]["hosted_supabase_live_readiness"]["surface"] == (
        "hosted_supabase"
    )
    incident = written["external_checks"]["live_incident_response_drill"]
    assert incident["channel_evidence"]["operator_ack"] is True
    acceptance = written["system_order_scope_acceptance"]
    assert acceptance["scope"] == "system_created_live_orders_only"
    assert acceptance["runtime_env_value_confirmed"] is True
    provider_evidence = written["provider_lifecycle_evidence"]
    assert provider_evidence["provider"] == "toss"
    provider_gap_evidence = written["provider_gap_evidence"]
    assert provider_gap_evidence["schema_version"] == 1
    scorecard = written["local_checks"]["live_readiness_scorecard"]
    assert scorecard["final_output"] == (
        "FINAL=PASS live_readiness_scorecard scorecard_security_scan=1 "
        "worklist_rows=46 candidate_findings=3 reportable_findings=0"
    )


def test_live_readiness_evidence_collector_passes_gap_and_scope_evidence_to_provider_gap_gate(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, output_path=tmp_path / "bundle.json")
    observed_command: tuple[str, ...] | None = None

    def runner(spec: CommandSpec) -> CommandResult:
        nonlocal observed_command
        if spec.name == "provider_contract_gaps":
            observed_command = spec.command
        return _pass_runner(spec)

    collect_live_readiness_evidence_bundle(
        config,
        command_runner=runner,
        git_evidence_reader=_git_evidence,
        clock=_clock(),
    )

    assert observed_command is not None
    assert "--system-order-scope-evidence" in observed_command
    evidence_arg_index = observed_command.index("--system-order-scope-evidence") + 1
    assert observed_command[evidence_arg_index] == str(config.system_order_scope_evidence)
    assert "--provider-gap-evidence" in observed_command
    provider_gap_arg_index = observed_command.index("--provider-gap-evidence") + 1
    assert observed_command[provider_gap_arg_index] == str(config.provider_gap_evidence)
    assert "--verify-remote-provider-gap-artifacts" not in observed_command


def test_live_readiness_evidence_collector_can_verify_remote_provider_gap_artifacts(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path, output_path=tmp_path / "bundle.json"),
        verify_remote_provider_gap_artifacts=True,
    )
    observed_command: tuple[str, ...] | None = None

    def runner(spec: CommandSpec) -> CommandResult:
        nonlocal observed_command
        if spec.name == "provider_contract_gaps":
            observed_command = spec.command
        return _pass_runner(spec)

    collect_live_readiness_evidence_bundle(
        config,
        command_runner=runner,
        git_evidence_reader=_git_evidence,
        clock=_clock(),
    )

    assert observed_command is not None
    assert "--verify-remote-provider-gap-artifacts" in observed_command


def test_live_readiness_evidence_collector_accepts_production_readiness_live_evidence(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["deployment_environment"] = "production"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    provider_payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    provider_payload["environment"] = "live"
    provider_evidence.write_text(json.dumps(provider_payload), encoding="utf-8")

    bundle, summary = collect_live_readiness_evidence_bundle(
        _config(
            tmp_path,
            environment="production-readiness",
            output_path=output_path,
            provider_evidence=provider_evidence,
            system_order_scope_evidence=scope_evidence,
        ),
        command_runner=_live_provider_runner,
        git_evidence_reader=_git_evidence,
        clock=_clock(),
    )

    acceptance = bundle["system_order_scope_acceptance"]
    assert summary.system_order_scope_accepted is True
    assert bundle["environment"] == "production-readiness"
    assert isinstance(acceptance, dict)
    assert acceptance["deployment_environment"] == "production"
    assert output_path.exists()


def test_live_readiness_evidence_collector_rejects_production_readiness_sandbox_provider(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["deployment_environment"] = "production"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                environment="production-readiness",
                output_path=output_path,
                system_order_scope_evidence=scope_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "external_checks.provider_lifecycle_evidence.environment_must_match_bundle_environment"
    ) in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_hosted_skip(tmp_path: Path) -> None:
    output_path = tmp_path / "bundle.json"

    def runner(spec: CommandSpec) -> CommandResult:
        if spec.name == "hosted_supabase_live_readiness":
            return CommandResult(
                2,
                "FINAL=SKIP hosted_supabase_env_missing missing=SUPABASE_URL\n",
                "",
            )
        return _pass_runner(spec)

    with pytest.raises(
        CollectorError,
        match="hosted_supabase_live_readiness_command_returncode_nonzero",
    ):
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, output_path=output_path),
            command_runner=runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_existing_output_artifact(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    original_output = "previous retained bundle that must not be overwritten\n"
    output_path.write_text(original_output, encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, output_path=output_path),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "collector_output_path_must_not_exist"
    assert "previous retained bundle" not in reason
    assert output_path.read_text(encoding="utf-8") == original_output


def test_live_readiness_evidence_collector_rejects_output_created_during_collection(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    raced_output = "concurrent retained bundle that must not be overwritten\n"

    def runner(spec: CommandSpec) -> CommandResult:
        if spec.name == "provider_contract_gaps":
            output_path.write_text(raced_output, encoding="utf-8")
        return _pass_runner(spec)

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, output_path=output_path),
            command_runner=runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "collector_output_path_must_not_exist"
    assert "concurrent retained bundle" not in reason
    assert output_path.read_text(encoding="utf-8") == raced_output


def test_live_readiness_evidence_collector_reports_unwritable_output_without_path(
    tmp_path: Path,
) -> None:
    missing_dir = tmp_path / "missing-output-dir"
    output_path = missing_dir / "bundle.json"

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, output_path=output_path),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "collector_output_path_unwritable"
    assert "missing-output-dir" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_nonzero_command_with_pass_output(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"

    def runner(spec: CommandSpec) -> CommandResult:
        if spec.name == "live_alert_drill":
            return CommandResult(
                1,
                "FINAL=PASS live_external_alert_drill delivered=4 max_latency_ms=17\n",
                "Traceback leaked-secret-that-must-not-print\n",
            )
        return _pass_runner(spec)

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, output_path=output_path),
            command_runner=runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "live_alert_drill_command_returncode_nonzero"
    assert "FINAL=PASS" not in reason
    assert "leaked-secret-that-must-not-print" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_non_final_command_output(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"

    def runner(spec: CommandSpec) -> CommandResult:
        if spec.name == "live_alert_drill":
            return CommandResult(
                0,
                (
                    "debug leaked-secret-that-must-not-print\n"
                    "FINAL=PASS live_external_alert_drill delivered=4 max_latency_ms=17\n"
                ),
                "",
            )
        return _pass_runner(spec)

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, output_path=output_path),
            command_runner=runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "live_alert_drill_non_final_output_lines_not_allowed"
    assert "leaked-secret-that-must-not-print" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_non_final_incident_output_file(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    config = _config(tmp_path, output_path=output_path)
    config.incident_output_file.write_text(
        "debug leaked-secret-that-must-not-print\n"
        "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=17 "
        "acknowledged=true ack_latency_ms=2300 drill_id=incident-drill-20260628-1\n",
        encoding="utf-8",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "live_incident_response_drill_non_final_output_lines_not_allowed"
    assert "leaked-secret-that-must-not-print" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_multiple_final_lines(tmp_path: Path) -> None:
    def runner(spec: CommandSpec) -> CommandResult:
        if spec.name == "live_recovery_drill":
            return CommandResult(0, "FINAL=PASS one\nFINAL=PASS two\n", "")
        return _pass_runner(spec)

    with pytest.raises(CollectorError, match="live_recovery_drill_final_line_count=2"):
        collect_live_readiness_evidence_bundle(
            _config(tmp_path),
            command_runner=runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )


def test_live_readiness_evidence_collector_rejects_sensitive_security_summary(
    tmp_path: Path,
) -> None:
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["access_token"] = "secret-value-that-must-not-print"
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, security_summary=security_summary),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "sensitive_key_not_allowed:security_scan_summary.access_token" in reason
    assert "secret-value-that-must-not-print" not in reason


def test_live_readiness_evidence_collector_rejects_weak_security_report_artifact(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["report_uri"] = "https://example.test/mock-security-report?token=abc"
    payload["report_sha256"] = "not-a-sha"
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_uri_must_not_be_mock_fixture_or_local" in reason
    assert "security_scan_evidence.report_uri_must_not_include_query_or_fragment" in reason
    assert "security_scan_evidence.report_sha256_must_be_64_hex" in reason
    assert "token=abc" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_output_overwriting_input_artifact(
    tmp_path: Path,
) -> None:
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    original_provider_evidence = provider_evidence.read_text(encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=provider_evidence,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "collector_artifact_paths_must_be_distinct"
    assert "provider-evidence" not in reason
    assert provider_evidence.read_text(encoding="utf-8") == original_provider_evidence


def test_live_readiness_evidence_collector_rejects_output_overwriting_security_report(
    tmp_path: Path,
) -> None:
    security_summary = _write_security_summary(tmp_path)
    security_report = tmp_path / "security-report.md"
    original_security_report = security_report.read_text(encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=security_report,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
    )

    reason = str(exc_info.value)
    assert reason == "collector_output_path_must_not_exist"
    assert "security-report" not in reason
    assert security_report.read_text(encoding="utf-8") == original_security_report


def test_live_readiness_evidence_collector_rejects_duplicate_input_artifact_paths(
    tmp_path: Path,
) -> None:
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                provider_evidence=provider_evidence,
                incident_channel_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "collector_artifact_paths_must_be_distinct"
    assert "provider-evidence" not in reason


def test_live_readiness_evidence_collector_rejects_sensitive_incident_evidence(
    tmp_path: Path,
) -> None:
    incident_evidence = _write_incident_channel_evidence(tmp_path)
    payload = json.loads(incident_evidence.read_text(encoding="utf-8"))
    payload["webhook_token"] = "incident-secret-that-must-not-print"
    incident_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, incident_channel_evidence=incident_evidence),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "sensitive_key_not_allowed:incident_channel_evidence.webhook_token" in reason
    assert "incident-secret-that-must-not-print" not in reason


def test_live_readiness_evidence_collector_rejects_automated_incident_ack(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    incident_evidence = _write_incident_channel_evidence(tmp_path)
    payload = json.loads(incident_evidence.read_text(encoding="utf-8"))
    payload["operator_ack_by"] = "incident-script-bot"
    incident_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                incident_channel_evidence=incident_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "incident_response_evidence.channel_evidence.operator_ack_by_must_be_human" in reason
    assert "incident-script-bot" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_incident_capture_not_after_ack(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    incident_evidence = _write_incident_channel_evidence(tmp_path)
    payload = json.loads(incident_evidence.read_text(encoding="utf-8"))
    payload["captured_at"] = "2026-06-28T01:04:12Z"
    payload["operator_ack_at"] = "2026-06-28T01:04:12Z"
    incident_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                incident_channel_evidence=incident_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence."
        "captured_at_must_be_after_operator_ack_at"
    ) in reason
    assert "2026-06-28T01" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_automated_bundle_reviewer(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    config = replace(
        _config(tmp_path, output_path=output_path),
        reviewed_by="release-ci-bot",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "bundle.reviewed_by_must_be_human" in reason
    assert "release-ci-bot" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_contact_like_bundle_reviewer(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    config = replace(
        _config(tmp_path, output_path=output_path),
        reviewed_by="release-admin@example.com",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "bundle.reviewed_by_must_be_logical_operator_id" in reason
    assert "release-admin@example.com" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_scope_acceptance_self_review(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    config = replace(
        _config(tmp_path, output_path=output_path),
        reviewed_by="ops-admin-1",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "bundle.reviewed_by_must_differ_from_system_order_scope_accepted_by"
    assert "ops-admin-1" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_incident_ack_self_review(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    config = replace(
        _config(tmp_path, output_path=output_path),
        reviewed_by="ops-admin-2",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "bundle.reviewed_by_must_differ_from_incident_ack_operator"
    assert "ops-admin-2" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_lifecycle_self_review(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    config = replace(
        _config(tmp_path, output_path=output_path),
        reviewed_by="provider-admin-1",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "bundle.reviewed_by_must_differ_from_provider_lifecycle_reviewer"
    assert "provider-admin-1" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_lifecycle_internal_self_review(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    audit = cast(dict[str, object], payload["audit"])
    audit["reviewed_by"] = "provider.admin.1"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.operator_reviewed_by_must_differ_from_audit_reviewed_by"
        in reason
    )
    assert "provider.admin.1" not in reason
    assert "provider-admin-1" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_lifecycle_audit_before_recovery(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    audit = cast(dict[str, object], payload["audit"])
    audit["reviewed_at"] = "2026-06-28T01:06:30Z"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "audit.reviewed_at_must_be_after_unknown_recovery_operator_reviewed_at"
        in reason
    )
    assert "2026-06-28T01" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_recovery_before_latest_status(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    unknown_recovery = cast(dict[str, object], payload["unknown_recovery"])
    unknown_recovery["operator_reviewed_at"] = "2026-06-28T01:04:15Z"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.operator_reviewed_at_must_be_after_latest_provider_status_observed_at"
        in reason
    )
    assert "2026-06-28T01" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_status_regression_after_terminal(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    status_sequence = cast(list[dict[str, object]], payload["provider_status_sequence"])
    status_sequence.append(
        {
            "observed_at": "2026-06-28T01:05:30Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "PENDING",
            "local_status": "sent",
        }
    )
    artifacts = cast(list[dict[str, object]], payload["evidence_artifacts"])
    artifacts[1]["captured_at"] = "2026-06-28T01:06:00Z"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "provider_status_sequence[2].provider_status_must_not_regress_after_terminal"
        in reason
    )
    assert "PENDING" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_local_status_regression_after_terminal(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    status_sequence = cast(list[dict[str, object]], payload["provider_status_sequence"])
    status_sequence.append(
        {
            "observed_at": "2026-06-28T01:05:30Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "CANCELED",
            "local_status": "sent",
        }
    )
    artifacts = cast(list[dict[str, object]], payload["evidence_artifacts"])
    artifacts[1]["captured_at"] = "2026-06-28T01:06:00Z"
    artifacts[2]["captured_at"] = "2026-06-28T01:06:00Z"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "provider_status_sequence[2].local_status_must_not_regress_after_terminal"
        in reason
    )
    assert "sent" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_create_status_first_observation_mismatch(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    created_order = cast(dict[str, object], payload["created_order"])
    created_order["status_after_create"] = "unknown_requires_manual_check"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "created_order.status_after_create_must_match_first_local_status"
        in reason
    )
    assert "unknown_requires_manual_check" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_unknown_recovery_for_different_order(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    unknown_recovery = cast(dict[str, object], payload["unknown_recovery"])
    unknown_recovery["order_id"] = "99999999-9999-4999-8999-999999999999"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.order_id_must_match_created_order"
        in reason
    )
    assert "99999999" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_unknown_recovery_final_status_mismatch(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    unknown_recovery = cast(dict[str, object], payload["unknown_recovery"])
    unknown_recovery["final_status"] = "filled"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence."
        "unknown_recovery.final_status_must_match_latest_local_status"
        in reason
    )
    assert "filled" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_incident_operator_reuse(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    incident_evidence = _write_incident_channel_evidence(tmp_path)
    payload = json.loads(incident_evidence.read_text(encoding="utf-8"))
    payload["operator_ack_by"] = "provider-admin-1"
    incident_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                incident_channel_evidence=incident_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "bundle.evidence_operator_roles_must_be_distinct"
    assert "provider-admin-1" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_scope_operator_reuse(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["accepted_by"] = "provider-admin-1"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")
    config = replace(
        _config(
            tmp_path,
            output_path=output_path,
            system_order_scope_evidence=scope_evidence,
        ),
        system_order_scope_accepted_by="provider-admin-1",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "bundle.evidence_operator_roles_must_be_distinct"
    assert "provider-admin-1" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_incident_scope_operator_reuse(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    incident_evidence = _write_incident_channel_evidence(tmp_path)
    payload = json.loads(incident_evidence.read_text(encoding="utf-8"))
    payload["operator_ack_by"] = "ops-admin-1"
    incident_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                incident_channel_evidence=incident_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "bundle.evidence_operator_roles_must_be_distinct"
    assert "ops-admin-1" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_automated_provider_operator(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    unknown_recovery = cast(dict[str, object], payload["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "provider-script-bot"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.unknown_recovery.operator_reviewed_by_must_be_human" in reason
    )
    assert "provider-script-bot" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_provider_raw_identifier_redaction(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    created_order = cast(dict[str, object], payload["created_order"])
    created_order["provider_order_id_redacted"] = (
        "redacted:raw-provider-order-1234567890"
    )
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.created_order."
        "provider_order_id_redacted_must_use_allowed_redaction_format"
    ) in reason
    assert "raw-provider-order-1234567890" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_contact_like_provider_operator(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    provider_evidence = _write_provider_lifecycle_evidence(tmp_path)
    payload = json.loads(provider_evidence.read_text(encoding="utf-8"))
    unknown_recovery = cast(dict[str, object], payload["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "provider-admin@example.com"
    audit = cast(dict[str, object], payload["audit"])
    audit["reviewed_by"] = "https://ops.example.com/users/provider-admin"
    provider_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                provider_evidence=provider_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "provider_lifecycle_evidence.unknown_recovery."
        "operator_reviewed_by_must_be_logical_operator_id"
    ) in reason
    assert "provider_lifecycle_evidence.audit.reviewed_by_must_be_logical_operator_id" in reason
    assert "provider-admin@example.com" not in reason
    assert "ops.example.com" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_scope_acceptance_mismatch(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["accepted_by"] = "different-admin"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                system_order_scope_evidence=scope_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "system_order_scope_evidence_accepted_by_mismatch"
    assert "different-admin" not in reason
    assert "ops-admin-1" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_automated_scope_acceptance_operator(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["accepted_by"] = "scope-system-bot"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")
    config = replace(
        _config(
            tmp_path,
            output_path=output_path,
            system_order_scope_evidence=scope_evidence,
        ),
        system_order_scope_accepted_by="scope-system-bot",
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.accepted_by_must_be_human" in reason
    assert "scope-system-bot" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_scope_capture_not_after_acceptance(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["accepted_at"] = "2026-06-28T01:09:00Z"
    payload["evidence_captured_at"] = "2026-06-28T01:09:00Z"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                system_order_scope_evidence=scope_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "system_order_scope_evidence.evidence_captured_at_must_be_after_accepted_at"
        in reason
    )
    assert "2026-06-28T01" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_scope_environment_mismatch(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["deployment_environment"] = "production"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                system_order_scope_evidence=scope_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "system_order_scope_acceptance.deployment_environment_must_match_bundle_environment"
    ) in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_sensitive_scope_evidence(
    tmp_path: Path,
) -> None:
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["operator_jwt"] = "scope-secret-that-must-not-print"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, system_order_scope_evidence=scope_evidence),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "sensitive_key_not_allowed:system_order_scope_evidence.operator_jwt" in reason
    assert "scope-secret-that-must-not-print" not in reason


def test_live_readiness_evidence_collector_rejects_weak_scope_evidence(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["evidence_uri"] = "https://example.test/mock-scope?token=abc"
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                system_order_scope_evidence=scope_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_must_not_be_mock_or_fixture" in reason
    assert "system_order_scope_evidence.evidence_uri_must_not_include_query_or_fragment" in reason
    assert "token=abc" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_scope_uri_path_traversal(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    payload = json.loads(scope_evidence.read_text(encoding="utf-8"))
    payload["evidence_uri"] = (
        "https://evidence.kr-autotrading.net/approvals/%2e%2e/scope.json"
    )
    scope_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                system_order_scope_evidence=scope_evidence,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "system_order_scope_evidence.evidence_uri_must_not_include_path_traversal"
        in reason
    )
    assert "%2e%2e" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_stale_security_scan_binding(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["source_diff_sha256"] = "f" * 64
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert reason == "security_scan_summary_source_diff_sha256_mismatch"
    assert "expected=" not in reason
    assert "actual=" not in reason
    assert "f" * 64 not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_malicious_source_binding_without_leak(
    tmp_path: Path,
) -> None:
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["source_diff_sha256"] = "secret_value_that_must_not_print"
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(tmp_path, security_summary=security_summary),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "security_scan_evidence.source_diff_sha256_must_be_64_hex" in reason
    assert "secret_value_that_must_not_print" not in reason


def test_live_readiness_evidence_collector_rejects_path_like_security_scan_id(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["scan_id"] = "C:security-scan-20260628"
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "security_scan_evidence.scan_id_must_be_logical_identifier" in reason
    assert "C:security-scan-20260628" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_security_report_uri_path_traversal(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["report_uri"] = (
        "https://evidence.kr-autotrading.net/security-scans/"
        "msp-20260628/%252e%252e/report.md"
    )
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert (
        "security_scan_evidence.report_uri_must_not_include_path_traversal"
        in reason
    )
    assert "%252e%252e" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_excludes_security_report_path_from_source_hash(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ops@example.test")
    _git(repo, "config", "user.name", "Ops Test")
    tracked_source = repo / "tracked.py"
    tracked_source.write_text("print('stable')\n", encoding="utf-8")
    tracked_report = repo / "security-report.md"
    tracked_report.write_text("# Previous report\n", encoding="utf-8")
    _git(repo, "add", "tracked.py", "security-report.md")
    _git(repo, "commit", "-m", "init")

    config = _config(repo, output_path=repo / "bundle.json")
    security_summary = config.security_scan_summary
    tracked_report.write_text(
        "# Codex Security report\n\nNo reportable findings.\n",
        encoding="utf-8",
    )
    expected_git = _collect_git_evidence(
        repo,
        excluded_paths=(
                config.provider_evidence,
                config.provider_gap_evidence,
                config.incident_output_file,
            config.incident_channel_evidence,
            config.security_scan_summary,
            config.system_order_scope_evidence,
            config.output,
            tracked_report,
        ),
    )
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["report_path"] = "security-report.md"
    payload["report_sha256"] = hashlib.sha256(tracked_report.read_bytes()).hexdigest()
    payload["source_head"] = expected_git["source_head"]
    payload["source_diff_sha256"] = expected_git["source_diff_sha256"]
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    _, summary = collect_live_readiness_evidence_bundle(
        config,
        command_runner=_pass_runner,
        clock=_clock(),
    )

    assert summary.security_scan is True
    assert config.output.exists()

    tracked_source.write_text("print('changed')\n", encoding="utf-8")
    config.output.unlink()
    with pytest.raises(
        CollectorError,
        match="security_scan_summary_source_diff_sha256_mismatch",
    ):
        collect_live_readiness_evidence_bundle(
            config,
            command_runner=_pass_runner,
            clock=_clock(),
        )


def test_live_readiness_evidence_collector_rejects_stale_security_report_hash(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["report_sha256"] = "f" * 64
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError, match="report_sha256_mismatch"):
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_absolute_security_report_path(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["report_path"] = "C:/Users/choey/.tmp/security-report.md"
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_path_must_be_relative_retained_path" in reason
    assert "C:/Users/choey" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_rejects_security_report_path_escape(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_report = outside_dir / "security-report.md"
    outside_report.write_text(
        "# Codex Security report\n\nNo reportable findings.\n",
        encoding="utf-8",
    )
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["report_path"] = "../outside/security-report.md"
    payload["report_sha256"] = hashlib.sha256(outside_report.read_bytes()).hexdigest()
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            git_evidence_reader=_git_evidence,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_path_must_stay_under_evidence_dir" in reason
    assert "outside" not in reason
    assert not output_path.exists()


def test_live_readiness_evidence_collector_validates_security_report_before_source_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "bundle.json"
    security_summary = _write_security_summary(tmp_path)
    payload = json.loads(security_summary.read_text(encoding="utf-8"))
    payload["report_path"] = "../outside/security-report.md"
    security_summary.write_text(json.dumps(payload), encoding="utf-8")

    def fail_source_binding(
        _repo_root: Path,
        **_kwargs: object,
    ) -> dict[str, str]:
        raise AssertionError("source binding must not run before report validation")

    monkeypatch.setattr(
        "app.tools.collect_live_readiness_evidence_bundle._collect_git_evidence",
        fail_source_binding,
    )

    with pytest.raises(CollectorError) as exc_info:
        collect_live_readiness_evidence_bundle(
            _config(
                tmp_path,
                output_path=output_path,
                security_summary=security_summary,
            ),
            command_runner=_pass_runner,
            clock=_clock(),
        )

    reason = str(exc_info.value)
    assert "security_scan_evidence.report_path_must_stay_under_evidence_dir" in reason
    assert "source binding must not run" not in reason
    assert not output_path.exists()


def test_git_evidence_excludes_retained_artifacts_from_source_hash(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ops@example.test")
    _git(repo, "config", "user.name", "Ops Test")
    tracked = repo / "tracked.py"
    tracked.write_text("print('stable')\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")

    retained_evidence = repo / "security-summary.json"
    retained_evidence.write_text("one", encoding="utf-8")
    first = _collect_git_evidence(repo, excluded_paths=(retained_evidence,))
    retained_evidence.write_text("two", encoding="utf-8")
    second = _collect_git_evidence(repo, excluded_paths=(retained_evidence,))

    source_file = repo / "new_source.py"
    source_file.write_text("print('new')\n", encoding="utf-8")
    third = _collect_git_evidence(repo, excluded_paths=(retained_evidence,))

    assert first == second
    assert third["source_diff_sha256"] != first["source_diff_sha256"]


def test_git_evidence_excludes_tracked_retained_artifacts_from_source_hash(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ops@example.test")
    _git(repo, "config", "user.name", "Ops Test")
    tracked = repo / "tracked.py"
    tracked.write_text("print('stable')\n", encoding="utf-8")
    retained_evidence = repo / "security-summary.json"
    retained_evidence.write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.py", "security-summary.json")
    _git(repo, "commit", "-m", "init")

    first = _collect_git_evidence(repo, excluded_paths=(retained_evidence,))
    retained_evidence.write_text("two\n", encoding="utf-8")
    second = _collect_git_evidence(repo, excluded_paths=(retained_evidence,))
    tracked.write_text("print('changed')\n", encoding="utf-8")
    third = _collect_git_evidence(repo, excluded_paths=(retained_evidence,))

    assert first == second
    assert third["source_diff_sha256"] != first["source_diff_sha256"]


def test_git_evidence_normalizes_subdirectory_to_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "apps" / "worker"
    nested.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "ops@example.test")
    _git(repo, "config", "user.name", "Ops Test")
    tracked = repo / "tracked.py"
    tracked.write_text("print('stable')\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")

    first = _collect_git_evidence(nested)
    root_untracked = repo / "root_untracked.py"
    root_untracked.write_text("print('root change')\n", encoding="utf-8")
    second = _collect_git_evidence(nested)

    assert first["source_head"] == second["source_head"]
    assert second["source_diff_sha256"] != first["source_diff_sha256"]


def _config(
    tmp_path: Path,
    *,
    environment: str = "staging",
    output_path: Path | None = None,
    security_summary: Path | None = None,
    provider_evidence: Path | None = None,
    provider_gap_evidence: Path | None = None,
    incident_channel_evidence: Path | None = None,
    system_order_scope_evidence: Path | None = None,
) -> CollectorConfig:
    incident_output = tmp_path / "incident-output.txt"
    incident_output.write_text(
        "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=17 "
        "acknowledged=true ack_latency_ms=2300 drill_id=incident-drill-20260628-1\n",
        encoding="utf-8",
    )
    return CollectorConfig(
        repo_root=tmp_path,
        environment=environment,
        reviewed_by="release-admin-1",
        provider_evidence=provider_evidence or _write_provider_lifecycle_evidence(tmp_path),
        provider_gap_evidence=provider_gap_evidence or _write_provider_gap_evidence(tmp_path),
        incident_output_file=incident_output,
        incident_channel_evidence=(
            incident_channel_evidence or _write_incident_channel_evidence(tmp_path)
        ),
        security_scan_summary=security_summary or _write_security_summary(tmp_path),
        system_order_scope_evidence=(
            system_order_scope_evidence or _write_system_order_scope_evidence(tmp_path)
        ),
        system_order_scope_accepted_by="ops-admin-1",
        output=output_path or (tmp_path / "bundle.json"),
    )


def _write_security_summary(tmp_path: Path) -> Path:
    path = tmp_path / "security-summary.json"
    report_path = tmp_path / "security-report.md"
    report_content = "# Codex Security report\n\nNo reportable findings.\n"
    report_path.write_text(report_content, encoding="utf-8")
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "scan_id": "msp-20260628-independent-replay",
                "report_path": report_path.name,
                "report_uri": "https://evidence.kr-autotrading.net/security-scans/msp-20260628/report.md",
                "report_sha256": report_sha256,
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
        ),
        encoding="utf-8",
    )
    return path


def _write_system_order_scope_evidence(tmp_path: Path) -> Path:
    path = tmp_path / "system-order-scope-evidence.json"
    path.write_text(
        json.dumps(
            {
                "accepted": True,
                "scope": "system_created_live_orders_only",
                "broker": "toss",
                "limitation": "broker_wide_closed_order_history_unavailable",
                "runtime_env_var": "LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED",
                "runtime_env_value_confirmed": True,
                "deployment_environment": "staging",
                "accepted_by": "ops-admin-1",
                "accepted_at": "2026-06-28T01:08:59Z",
                "evidence_captured_at": "2026-06-28T01:09:00Z",
                "evidence_uri": "https://evidence.kr-autotrading.net/approvals/SCOPE-20260628-1",
                "evidence_sha256": (
                    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
                ),
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_provider_lifecycle_evidence(tmp_path: Path) -> Path:
    path = tmp_path / "provider-evidence.json"
    path.write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )
    return path


def _write_provider_gap_evidence(tmp_path: Path) -> Path:
    api_gaps_path = Path(__file__).resolve().parents[5] / "docs" / "API_GAPS.md"
    api_gaps_markdown = api_gaps_path.read_text(encoding="utf-8")
    gaps = evaluate_provider_api_gaps(api_gaps_markdown).gaps
    gap_ids = [provider_api_gap_id(gap) for gap in gaps]
    provider_gap_ids: dict[str, list[str]] = {}
    for gap in gaps:
        provider_gap_ids.setdefault(gap.provider, []).append(provider_api_gap_id(gap))
    path = tmp_path / "provider-gap-evidence.json"
    path.write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )
    return path


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


def _git_evidence(_repo_root: Path) -> dict[str, str]:
    return {
        "source_head": "a" * 40,
        "source_diff_sha256": "b" * 64,
    }


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"git {' '.join(args)} failed: stdout={completed.stdout} stderr={completed.stderr}"
        )


def _write_incident_channel_evidence(tmp_path: Path) -> Path:
    path = tmp_path / "incident-channel-evidence.json"
    path.write_text(
        json.dumps(
            {
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
            }
        ),
        encoding="utf-8",
    )
    return path


def _pass_runner(spec: CommandSpec) -> CommandResult:
    outputs = {
        "hosted_supabase_live_readiness": (
            "FINAL=PASS hosted_supabase_live_readiness postgrest=1 "
            "anon_rpc_denied=2 service_rpc_allowed=2 anon_table_denied=1 "
            "service_table_allowed=1 authenticated_table_allowed=2 realtime=1"
        ),
        "hosted_live_enable_flow": (
            "FINAL=PASS hosted_live_enable_flow requester_admin=1 reviewer_admin=1 "
            "request_created=1 self_review_denied=1 review_accepted=1 "
            "activation_consumed_once=1 second_activation_denied=1"
        ),
        "provider_lifecycle_evidence": (
            "FINAL=PASS provider_lifecycle_evidence provider=toss environment=sandbox "
            "status_observations=2 audit_logs_reviewed=2 evidence_artifacts=5"
        ),
        "worker_release_freshness": (
            "FINAL=PASS worker_release_freshness "
            "expected_sha_short=2bac8362b504 observed_sha_short=2bac8362b504 "
            "heartbeat_age_sec=12 max_age_sec=300"
        ),
        "live_enable_migration": "FINAL=PASS live_enable_consumed_once rpc_hardening",
        "live_execution_safety_drill": (
            "FINAL=PASS live_execution_safety_drill missing_evidence_blocked=1 "
            "pre_broker_manual_check=1 provider_result_recorded=1 duplicate_blocked=1 "
            "broker_calls=1"
        ),
        "live_recovery_drill": (
            "FINAL=PASS live_recovery_drill reconciled_updates=1 manual_check_events=2 "
            "cancel_confirmed=1 cancel_unknown=1 status_calls=4 cancel_calls=2 "
            "pending_order_blocked=1 manual_check_preserved=1"
        ),
        "live_alert_drill": "FINAL=PASS live_external_alert_drill delivered=4 max_latency_ms=17",
        "provider_contract_gaps": (
            "FINAL=PASS provider_contract_gaps total_gaps=18 "
            "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 "
            "warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:"
            "partial-system-only-accepted-fail-closed system_order_scope_accepted=1 "
            "provider_gap_evidence=1"
        ),
        "live_readiness_scorecard": (
            "FINAL=PASS live_readiness_scorecard scorecard_security_scan=1 "
            "worklist_rows=46 candidate_findings=3 reportable_findings=0"
        ),
    }
    return CommandResult(0, f"{outputs[spec.name]}\n", "")


def _live_provider_runner(spec: CommandSpec) -> CommandResult:
    result = _pass_runner(spec)
    if spec.name != "provider_lifecycle_evidence":
        return result
    return CommandResult(
        result.returncode,
        result.stdout.replace("environment=sandbox", "environment=live"),
        result.stderr,
    )


def _clock() -> Callable[[], datetime]:
    start = datetime(2026, 6, 28, 1, 0, tzinfo=UTC)
    times = (start + timedelta(minutes=offset) for offset in range(30))
    return iter(times).__next__

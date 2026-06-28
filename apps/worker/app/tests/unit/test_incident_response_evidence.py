from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.tools.verify_incident_response_evidence import (
    IncidentResponseEvidenceValidationError,
    main,
    verify_incident_response_evidence,
    verify_incident_response_evidence_files,
)


def test_incident_response_evidence_passes_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "incident-output.txt"
    channel_path = tmp_path / "incident-channel-evidence.json"
    output_path.write_text(_ack_output(), encoding="utf-8")
    channel_path.write_text(json.dumps(_valid_channel_evidence()), encoding="utf-8")

    exit_code = main(
        [
            "--incident-output-file",
            str(output_path),
            "--incident-channel-evidence",
            str(channel_path),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS incident_response_evidence delivered=4 max_latency_ms=17 "
        "ack_latency_ms=2300 channel=ops-live-incidents operator_ack_by=ops-admin-2"
    ) in output


def test_incident_response_evidence_files_verifies_remote_channel_evidence(
    tmp_path: Path,
) -> None:
    body = b"incident-channel-export"
    output_path = tmp_path / "incident-output.txt"
    channel_path = tmp_path / "incident-channel-evidence.json"
    evidence = _valid_channel_evidence()
    evidence["evidence_sha256"] = _real_sha256(body)
    output_path.write_text(_ack_output(), encoding="utf-8")
    channel_path.write_text(json.dumps(evidence), encoding="utf-8")
    calls: list[tuple[str, int]] = []

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        calls.append((uri, timeout_seconds))
        return body

    summary = verify_incident_response_evidence_files(
        incident_output_file=output_path,
        incident_channel_evidence=channel_path,
        verify_remote_channel_evidence=True,
        remote_fetcher=fetcher,
        remote_timeout_seconds=4,
    )

    assert summary.delivered == 4
    assert calls == [(str(evidence["evidence_uri"]), 4)]


def test_incident_response_evidence_cli_verifies_remote_channel_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"incident-channel-cli-export"
    output_path = tmp_path / "incident-output.txt"
    channel_path = tmp_path / "incident-channel-evidence.json"
    evidence = _valid_channel_evidence()
    evidence["evidence_sha256"] = _real_sha256(body)
    output_path.write_text(_ack_output(), encoding="utf-8")
    channel_path.write_text(json.dumps(evidence), encoding="utf-8")

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        assert timeout_seconds == 10
        assert uri == evidence["evidence_uri"]
        return body

    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._default_remote_evidence_fetcher",
        fetcher,
    )

    exit_code = main(
        [
            "--incident-output-file",
            str(output_path),
            "--incident-channel-evidence",
            str(channel_path),
            "--verify-remote-channel-evidence",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS incident_response_evidence" in output


def test_incident_response_evidence_remote_channel_rejects_sha_mismatch_without_leak() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_sha256"] = _real_sha256(b"different-incident-export")

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
            verify_remote_channel_evidence=True,
            remote_fetcher=lambda uri, timeout_seconds: b"incident-channel-export",
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence."
        "evidence_uri_remote_sha256_mismatch"
    ) in reason
    assert "incident-channel-export" not in reason
    assert "INC-20260628-1" not in reason


def test_incident_response_evidence_remote_channel_rejects_fetch_failure_without_leak() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_sha256"] = _real_sha256(b"incident-channel-export")

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        raise OSError("secret incident evidence token")

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
            verify_remote_channel_evidence=True,
            remote_fetcher=fetcher,
        )

    reason = str(exc_info.value)
    assert "incident_response_evidence.channel_evidence.evidence_uri_remote_fetch_failed" in reason
    assert "secret incident evidence token" not in reason
    assert "evidence.kr-autotrading.net" not in reason


def test_incident_response_evidence_remote_channel_rejects_github_blob_page() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/incidents/INC-20260628-1"
    )

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
            verify_remote_channel_evidence=True,
            remote_fetcher=lambda uri, timeout_seconds: b"unused",
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence."
        "evidence_uri_remote_must_reference_raw_artifact_bytes"
    ) in reason
    assert "github.com" not in reason
    assert "INC-20260628-1" not in reason


def test_incident_response_evidence_rejects_non_ack_dry_run() -> None:
    with pytest.raises(
        IncidentResponseEvidenceValidationError,
        match="incident_output_final_line_must_be_ack_gated_pass",
    ):
        verify_incident_response_evidence(
            incident_output=(
                "FINAL=PASS live_incident_delivery_drill delivered=4 "
                "max_latency_ms=17 ack_required=false\n"
            ),
            incident_channel_evidence=_valid_channel_evidence(),
        )


def test_incident_response_evidence_rejects_non_final_output_lines() -> None:
    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=(
                "debug leaked-secret-that-must-not-print\n"
                "FINAL=PASS live_incident_response_drill delivered=4 "
                "max_latency_ms=17 acknowledged=true ack_latency_ms=2300 "
                "drill_id=incident-drill-20260628-1\n"
            ),
            incident_channel_evidence=_valid_channel_evidence(),
        )

    reason = str(exc_info.value)
    assert reason == "incident_output_non_final_lines_not_allowed"
    assert "leaked-secret-that-must-not-print" not in reason


def test_incident_response_evidence_rejects_suffixed_ack_check_name() -> None:
    with pytest.raises(
        IncidentResponseEvidenceValidationError,
        match="incident_output_final_line_must_be_ack_gated_pass",
    ):
        verify_incident_response_evidence(
            incident_output=(
                "FINAL=PASS live_incident_response_drill_preview delivered=4 "
                "max_latency_ms=17 acknowledged=true ack_latency_ms=2300 "
                "drill_id=incident-drill-20260628-1\n"
            ),
            incident_channel_evidence=_valid_channel_evidence(),
        )


def test_incident_response_evidence_rejects_weak_channel_evidence() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_uri"] = "https://evidence.kr-autotrading.net/incidents/mock-INC-1?token=abc"
    evidence["evidence_sha256"] = "not-a-sha"
    evidence["operator_ack"] = False

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence.evidence_uri_must_not_be_mock_or_fixture"
    ) in reason
    assert (
        "incident_response_evidence.channel_evidence."
        "evidence_uri_must_not_include_query_or_fragment"
    ) in reason
    assert "incident_response_evidence.channel_evidence.evidence_sha256_must_be_64_hex" in reason
    assert "incident_response_evidence.channel_evidence.operator_ack_must_be_true" in reason
    assert "token=abc" not in reason


def test_incident_response_evidence_rejects_non_https_channel_evidence_uri() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_uri"] = "s3://ops-internal/incidents/INC-20260628-1"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert "incident_response_evidence.channel_evidence.evidence_uri_must_be_https_uri" in reason
    assert "ops-internal/incidents" not in reason


def test_incident_response_evidence_rejects_local_https_channel_evidence_host() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_uri"] = "https://10.0.0.5/incidents/INC-20260628-1"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence.evidence_uri_must_be_remote_retained_uri"
        in reason
    )
    assert "10.0.0.5" not in reason


def test_incident_response_evidence_rejects_non_global_https_channel_evidence_ip_host() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_uri"] = "https://100.64.0.5/incidents/INC-20260628-1"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence.evidence_uri_must_be_remote_retained_uri"
        in reason
    )
    assert "100.64.0.5" not in reason


def test_incident_response_evidence_rejects_invalid_dns_channel_evidence_host() -> None:
    evidence = _valid_channel_evidence()
    evidence["evidence_uri"] = "https://ops_.internal/incidents/INC-20260628-1"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence.evidence_uri_must_be_remote_retained_uri"
        in reason
    )
    assert "ops_.internal" not in reason


def test_incident_response_evidence_rejects_url_like_channel_name_without_leak() -> None:
    evidence = _valid_channel_evidence()
    evidence["channel_name"] = "https://hooks.slack.com/services/T000/B000/SECRET"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence."
        "channel_name_must_be_logical_identifier"
    ) in reason
    assert "hooks.slack.com" not in reason
    assert "SECRET" not in reason


def test_incident_response_evidence_rejects_channel_drill_id_mismatch() -> None:
    evidence = _valid_channel_evidence()
    evidence["drill_id"] = "incident-drill-different"

    with pytest.raises(
        IncidentResponseEvidenceValidationError,
        match="drill_id_must_match_incident_output",
    ):
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )


def test_incident_response_evidence_rejects_future_channel_timestamps() -> None:
    evidence = _valid_channel_evidence()
    evidence["captured_at"] = "2099-06-28T01:04:10Z"
    evidence["operator_ack_at"] = "2099-06-28T01:04:12Z"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence.captured_at_must_not_be_future"
        in reason
    )
    assert (
        "incident_response_evidence.channel_evidence.operator_ack_at_must_not_be_future"
        in reason
    )
    assert "2099" not in reason


def test_incident_response_evidence_rejects_capture_not_after_operator_ack() -> None:
    evidence = _valid_channel_evidence()
    evidence["captured_at"] = "2026-06-28T01:04:12Z"
    evidence["operator_ack_at"] = "2026-06-28T01:04:12Z"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence."
        "captured_at_must_be_after_operator_ack_at"
    ) in reason
    assert "2026-06-28T01" not in reason


def test_incident_response_evidence_rejects_invalid_drill_id() -> None:
    evidence = _valid_channel_evidence()
    evidence["drill_id"] = "bad/drill/id"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=(
                "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=17 "
                "acknowledged=true ack_latency_ms=2300 drill_id=bad/drill/id\n"
            ),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert "incident_response_evidence.drill_id_invalid" in reason
    assert "incident_response_evidence.channel_evidence.drill_id_invalid" in reason


def test_incident_response_evidence_rejects_sensitive_unknown_key_without_leak() -> None:
    evidence = _valid_channel_evidence()
    evidence["webhook_token"] = "secret-token-that-must-not-print"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "sensitive_key_not_allowed:"
        "incident_response_evidence.channel_evidence.webhook_token"
    ) in reason
    assert "incident_response_evidence.channel_evidence.unknown_keys=webhook_token" in reason
    assert "secret-token-that-must-not-print" not in reason


def test_incident_response_evidence_rejects_automated_ack_operator() -> None:
    evidence = _valid_channel_evidence()
    evidence["operator_ack_by"] = "incident-automation-bot"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert "incident_response_evidence.channel_evidence.operator_ack_by_must_be_human" in reason
    assert "incident-automation-bot" not in reason


def test_incident_response_evidence_rejects_email_like_ack_operator_without_leak() -> None:
    evidence = _valid_channel_evidence()
    evidence["operator_ack_by"] = "ops-admin@example.com"

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=_ack_output(),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert (
        "incident_response_evidence.channel_evidence."
        "operator_ack_by_must_be_logical_operator_id"
    ) in reason
    assert "ops-admin@example.com" not in reason


def test_incident_response_evidence_accepts_human_operator_with_ci_substring() -> None:
    evidence = _valid_channel_evidence()
    evidence["operator_ack_by"] = "alice.operator"

    summary = verify_incident_response_evidence(
        incident_output=_ack_output(),
        incident_channel_evidence=evidence,
    )

    assert summary.operator_ack_by == "alice.operator"


def test_incident_response_evidence_rejects_missing_required_metrics() -> None:
    evidence = _valid_channel_evidence()

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=(
                "FINAL=PASS live_incident_response_drill delivered=3 "
                "acknowledged=true ack_latency_ms=2300 drill_id=incident-drill-20260628-1\n"
            ),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert "incident_response_evidence.delivered_must_be_4" in reason
    assert "incident_response_evidence.max_latency_ms_required_integer" in reason


def test_incident_response_evidence_rejects_slow_delivery() -> None:
    evidence = _valid_channel_evidence()

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=(
                "FINAL=PASS live_incident_response_drill delivered=4 "
                "max_latency_ms=2001 acknowledged=true ack_latency_ms=2300 "
                "drill_id=incident-drill-20260628-1\n"
            ),
            incident_channel_evidence=evidence,
        )

    assert "incident_response_evidence.max_latency_ms_above_2000" in str(exc_info.value)


def test_incident_response_evidence_rejects_unknown_or_duplicate_final_metrics() -> None:
    evidence = _valid_channel_evidence()

    with pytest.raises(IncidentResponseEvidenceValidationError) as exc_info:
        verify_incident_response_evidence(
            incident_output=(
                "FINAL=PASS live_incident_response_drill delivered=4 "
                "max_latency_ms=17 acknowledged=true ack_latency_ms=2300 "
                "drill_id=incident-drill-20260628-1 webhook_url=https://secret.invalid "
                "delivered=4\n"
            ),
            incident_channel_evidence=evidence,
        )

    reason = str(exc_info.value)
    assert "incident_response_evidence.final_output_unknown_metrics=webhook_url" in reason
    assert "incident_response_evidence.final_output_duplicate_metrics=delivered" in reason
    assert "secret.invalid" not in reason


def test_incident_response_evidence_requires_exact_acknowledged_true() -> None:
    evidence = _valid_channel_evidence()

    with pytest.raises(
        IncidentResponseEvidenceValidationError,
        match="incident_response_evidence.acknowledged_true_required",
    ):
        verify_incident_response_evidence(
            incident_output=(
                "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=17 "
                "acknowledged=truex ack_latency_ms=2300 "
                "drill_id=incident-drill-20260628-1\n"
            ),
            incident_channel_evidence=evidence,
        )


def _ack_output() -> str:
    return (
        "FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=17 "
        "acknowledged=true ack_latency_ms=2300 drill_id=incident-drill-20260628-1\n"
    )


def _valid_channel_evidence() -> dict[str, object]:
    return copy.deepcopy(
        {
            "captured_at": "2026-06-28T01:04:20Z",
            "channel_name": "ops-live-incidents",
            "drill_id": "incident-drill-20260628-1",
            "evidence_uri": "https://evidence.kr-autotrading.net/incidents/INC-20260628-1",
            "evidence_sha256": ("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
            "operator_ack": True,
            "operator_ack_at": "2026-06-28T01:04:12Z",
            "operator_ack_by": "ops-admin-2",
        }
    )


def _real_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()

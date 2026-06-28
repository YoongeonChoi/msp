from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from app.tools.verify_provider_lifecycle_evidence import (
    EvidenceValidationError,
    main,
    verify_provider_lifecycle_evidence,
    verify_provider_lifecycle_evidence_file,
    verify_provider_lifecycle_remote_artifacts,
)


def test_provider_lifecycle_evidence_passes_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = _write_evidence(tmp_path, _valid_evidence())

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS provider_lifecycle_evidence provider=toss environment=sandbox "
        "status_observations=2 audit_logs_reviewed=2 evidence_artifacts=5"
    ) in output


def test_provider_lifecycle_evidence_rejects_sensitive_keys_without_leaking_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = _valid_evidence()
    audit = cast(dict[str, object], evidence["audit"])
    audit["access_token"] = "secret-token-that-must-not-print"
    evidence_path = _write_evidence(tmp_path, evidence)

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL=FAIL provider_lifecycle_evidence" in output
    assert "sensitive_key_not_allowed:evidence.audit.access_token" in output
    assert "secret-token-that-must-not-print" not in output


def test_provider_lifecycle_evidence_rejects_unknown_root_and_order_keys() -> None:
    evidence = _valid_evidence()
    evidence["raw_provider_payload"] = {"body": "must stay in retained artifacts"}
    created_order = cast(dict[str, object], evidence["created_order"])
    created_order["raw_order_response"] = "must not be embedded"
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence[0]["broker_account_snapshot"] = "must not be embedded"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence.unknown_keys=raw_provider_payload" in reason
    assert "created_order.unknown_keys=raw_order_response" in reason
    assert "provider_status_sequence[0].unknown_keys=broker_account_snapshot" in reason
    assert "must not be embedded" not in reason


def test_provider_lifecycle_evidence_rejects_unknown_recovery_audit_and_artifact_keys() -> None:
    evidence = _valid_evidence()
    cancel_probe = cast(dict[str, object], evidence["cancel_probe"])
    cancel_probe["raw_cancel_response"] = "must stay out of release evidence"
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_chat_export"] = "retain externally by URI and SHA"
    audit = cast(dict[str, object], evidence["audit"])
    audit["raw_audit_rows"] = ["must stay out of release evidence"]
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["raw_artifact_bytes"] = "must stay in artifact storage"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "cancel_probe.unknown_keys=raw_cancel_response" in reason
    assert "unknown_recovery.unknown_keys=operator_chat_export" in reason
    assert "audit.unknown_keys=raw_audit_rows" in reason
    assert "evidence_artifacts[0].unknown_keys=raw_artifact_bytes" in reason
    assert "must stay out of release evidence" not in reason


def test_provider_lifecycle_evidence_rejects_unredacted_provider_identifier() -> None:
    evidence = _valid_evidence()
    created_order = cast(dict[str, object], evidence["created_order"])
    created_order["provider_order_id_redacted"] = "raw-provider-order-1234567890"

    with pytest.raises(
        EvidenceValidationError,
        match="provider_order_id_redacted_must_be_redacted",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_raw_identifier_after_redacted_prefix() -> None:
    evidence = _valid_evidence()
    created_order = cast(dict[str, object], evidence["created_order"])
    created_order["provider_order_id_redacted"] = (
        "redacted:raw-provider-order-1234567890"
    )

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "created_order.provider_order_id_redacted_must_use_allowed_redaction_format" in reason
    assert "raw-provider-order-1234567890" not in reason


def test_provider_lifecycle_evidence_rejects_missing_terminal_and_cancel_confirmation() -> None:
    evidence = _valid_evidence()
    evidence["provider_status_sequence"] = [
        {
            "observed_at": "2026-06-28T00:01:00Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "PENDING",
            "local_status": "sent",
        },
        {
            "observed_at": "2026-06-28T00:02:00Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "PENDING_REPLACE",
            "local_status": "partial_filled",
        },
    ]
    cancel_probe = cast(dict[str, object], evidence["cancel_probe"])
    cancel_probe["attempted"] = False
    cancel_probe["provider_final_status"] = "SENT"
    cancel_probe["local_status"] = "sent"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "provider_status_sequence_missing_terminal_provider_status" in reason
    assert "provider_status_sequence_missing_terminal_local_status" in reason
    assert "cancel_probe.attempted_must_be_true" in reason
    assert "cancel_probe.provider_final_status_must_be_uppercase_canceled" in reason
    assert "cancel_probe.local_status_must_be_canceled" in reason


def test_provider_lifecycle_evidence_rejects_lowercase_cancel_provider_status() -> None:
    evidence = _valid_evidence()
    cancel_probe = cast(dict[str, object], evidence["cancel_probe"])
    cancel_probe["provider_final_status"] = "canceled"

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.provider_final_status_must_be_uppercase_canceled",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_cancel_probe_for_different_order() -> None:
    evidence = _valid_evidence()
    cancel_probe = cast(dict[str, object], evidence["cancel_probe"])
    cancel_probe["local_order_id"] = "22222222-2222-4222-8222-222222222222"

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.local_order_id_must_match_created_order",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_requires_canceled_status_observation_for_cancel() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence[1]["provider_status"] = "FILLED"
    status_sequence[1]["local_status"] = "filled"

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.missing_canceled_status_observation",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_canceled_status_before_cancel_attempt() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence[1]["observed_at"] = "2026-06-28T00:02:00Z"

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.canceled_status_observed_before_attempt",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_hidden_pre_attempt_canceled() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence.insert(
        1,
        {
            "observed_at": "2026-06-28T00:02:00Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "CANCELED",
            "local_status": "canceled",
        },
    )

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.canceled_status_observed_before_attempt",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_pre_cancel_terminal() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence.insert(
        1,
        {
            "observed_at": "2026-06-28T00:02:00Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "FILLED",
            "local_status": "filled",
        },
    )

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.irreversible_terminal_status_observed_before_attempt",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_mismatched_terminal_status_pair() -> None:
    evidence = _valid_evidence()
    evidence["provider_status_sequence"] = [
        {
            "observed_at": "2026-06-28T00:01:00Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "FILLED",
            "local_status": "canceled",
        },
        {
            "observed_at": "2026-06-28T00:02:00Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "CANCELED",
            "local_status": "filled",
        },
    ]

    with pytest.raises(
        EvidenceValidationError,
        match="provider_status_sequence_missing_matching_terminal_status_pair",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_unknown_provider_status() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence[0]["provider_status"] = "filled raw account 123"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "provider_status_sequence[0].provider_status_must_be_uppercase_known_status" in reason
    assert "provider_status_sequence[0].provider_status_unknown" in reason
    assert "raw account 123" not in reason


def test_provider_lifecycle_evidence_rejects_provider_status_regression_after_terminal() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence.append(
        {
            "observed_at": "2026-06-28T00:05:30Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "PENDING",
            "local_status": "sent",
        }
    )
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_at"] = "2026-06-28T00:07:00Z"
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[1]["captured_at"] = "2026-06-28T00:06:00Z"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "provider_status_sequence[2].provider_status_must_not_regress_after_terminal" in reason
    assert "PENDING" not in reason


def test_provider_lifecycle_evidence_rejects_local_status_regression_after_terminal() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence.append(
        {
            "observed_at": "2026-06-28T00:05:30Z",
            "local_order_id": "11111111-1111-4111-8111-111111111111",
            "provider_status": "CANCELED",
            "local_status": "sent",
        }
    )
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_at"] = "2026-06-28T00:07:00Z"
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[1]["captured_at"] = "2026-06-28T00:06:00Z"
    artifacts[2]["captured_at"] = "2026-06-28T00:06:00Z"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "provider_status_sequence[2].local_status_must_not_regress_after_terminal" in reason
    assert "sent" not in reason


def test_provider_lifecycle_evidence_rejects_status_observation_for_different_order() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence[1]["local_order_id"] = "99999999-9999-4999-8999-999999999999"

    with pytest.raises(
        EvidenceValidationError,
        match=r"provider_status_sequence\[1\].local_order_id_must_match_created_order",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_status_observation_before_create() -> None:
    evidence = _valid_evidence()
    created_order = cast(dict[str, object], evidence["created_order"])
    created_order["created_at"] = "2026-06-28T00:01:30Z"
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence[1]["observed_at"] = status_sequence[0]["observed_at"]

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "provider_status_sequence[0].observed_at_must_be_after_created_order" in reason
    assert "provider_status_sequence[1].observed_at_must_be_after_previous_observation" in reason


def test_provider_lifecycle_evidence_rejects_status_observation_at_create_time() -> None:
    evidence = _valid_evidence()
    status_sequence = cast(list[dict[str, object]], evidence["provider_status_sequence"])
    status_sequence[0]["observed_at"] = "2026-06-28T00:00:30Z"

    with pytest.raises(
        EvidenceValidationError,
        match=r"provider_status_sequence\[0\].observed_at_must_be_after_created_order",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_create_status_first_observation_mismatch() -> None:
    evidence = _valid_evidence()
    created_order = cast(dict[str, object], evidence["created_order"])
    created_order["status_after_create"] = "unknown_requires_manual_check"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "created_order.status_after_create_must_match_first_local_status" in reason
    assert "unknown_requires_manual_check" not in reason


def test_provider_lifecycle_evidence_rejects_cancel_probe_before_create() -> None:
    evidence = _valid_evidence()
    cancel_probe = cast(dict[str, object], evidence["cancel_probe"])
    cancel_probe["attempted_at"] = "2026-06-28T00:00:20Z"

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.attempted_at_must_be_after_created_order",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_live_gate_left_enabled() -> None:
    evidence = _valid_evidence()
    evidence["live_order_allowed_after"] = True

    with pytest.raises(EvidenceValidationError, match="live_order_allowed_after_must_be_false"):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_future_drill_window() -> None:
    evidence = _valid_evidence()
    evidence["started_at"] = "2099-06-28T00:00:00Z"
    evidence["completed_at"] = "2099-06-28T00:10:00Z"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence.started_at_must_not_be_future" in reason
    assert "evidence.completed_at_must_not_be_future" in reason
    assert "2099" not in reason


def test_provider_lifecycle_evidence_rejects_out_of_window_cancel_probe() -> None:
    evidence = _valid_evidence()
    cancel_probe = cast(dict[str, object], evidence["cancel_probe"])
    cancel_probe["attempted_at"] = "2026-06-28T00:20:00Z"

    with pytest.raises(
        EvidenceValidationError,
        match="cancel_probe.attempted_at_outside_drill_window",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_out_of_window_audit_review() -> None:
    evidence = _valid_evidence()
    audit = cast(dict[str, object], evidence["audit"])
    audit["reviewed_at"] = "2026-06-28T00:20:00Z"

    with pytest.raises(EvidenceValidationError, match="audit.reviewed_at_outside_drill_window"):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_audit_before_unknown_recovery_review() -> None:
    evidence = _valid_evidence()
    audit = cast(dict[str, object], evidence["audit"])
    audit["reviewed_at"] = "2026-06-28T00:06:30Z"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "audit.reviewed_at_must_be_after_unknown_recovery_operator_reviewed_at" in reason
    assert "2026-06-28T00" not in reason


def test_provider_lifecycle_evidence_rejects_recovery_review_before_latest_status() -> None:
    evidence = _valid_evidence()
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_at"] = "2026-06-28T00:04:15Z"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert (
        "unknown_recovery.operator_reviewed_at_must_be_after_latest_provider_status_observed_at"
        in reason
    )
    assert "2026-06-28T00" not in reason


def test_provider_lifecycle_evidence_rejects_unknown_recovery_for_different_order() -> None:
    evidence = _valid_evidence()
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["order_id"] = "99999999-9999-4999-8999-999999999999"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "unknown_recovery.order_id_must_match_created_order" in reason
    assert "99999999" not in reason


def test_provider_lifecycle_evidence_rejects_unknown_recovery_final_status_mismatch() -> None:
    evidence = _valid_evidence()
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["final_status"] = "filled"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "unknown_recovery.final_status_must_match_latest_local_status" in reason
    assert "filled" not in reason


def test_provider_lifecycle_evidence_rejects_automated_operator_reviews() -> None:
    evidence = _valid_evidence()
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "recovery-automation-bot"
    audit = cast(dict[str, object], evidence["audit"])
    audit["reviewed_by"] = "audit-ci-bot"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "unknown_recovery.operator_reviewed_by_must_be_human" in reason
    assert "audit.reviewed_by_must_be_human" in reason
    assert "recovery-automation-bot" not in reason
    assert "audit-ci-bot" not in reason


def test_provider_lifecycle_evidence_rejects_contact_like_operator_reviews_without_leak() -> None:
    evidence = _valid_evidence()
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "provider-admin@example.com"
    audit = cast(dict[str, object], evidence["audit"])
    audit["reviewed_by"] = "https://ops.example.com/users/provider-admin"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "unknown_recovery.operator_reviewed_by_must_be_logical_operator_id" in reason
    assert "audit.reviewed_by_must_be_logical_operator_id" in reason
    assert "provider-admin@example.com" not in reason
    assert "ops.example.com" not in reason


def test_provider_lifecycle_evidence_allows_distinct_human_operator_review_identities() -> None:
    evidence = _valid_evidence()
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "alice.operator"
    audit = cast(dict[str, object], evidence["audit"])
    audit["reviewed_by"] = "bob.operator"

    summary = verify_provider_lifecycle_evidence(evidence)

    assert summary.provider == "toss"
    assert summary.environment == "sandbox"


def test_provider_lifecycle_evidence_rejects_reused_operator_review_identity_without_leak() -> None:
    evidence = _valid_evidence()
    unknown_recovery = cast(dict[str, object], evidence["unknown_recovery"])
    unknown_recovery["operator_reviewed_by"] = "alice.operator"
    audit = cast(dict[str, object], evidence["audit"])
    audit["reviewed_by"] = "alice-operator"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert (
        "unknown_recovery.operator_reviewed_by_must_differ_from_audit_reviewed_by"
        in reason
    )
    assert "alice.operator" not in reason
    assert "alice-operator" not in reason


def test_provider_lifecycle_evidence_rejects_artifacts_not_after_bound_events() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["captured_at"] = "2026-06-28T00:00:30Z"
    artifacts[1]["captured_at"] = "2026-06-28T00:04:30Z"
    artifacts[2]["captured_at"] = "2026-06-28T00:04:30Z"
    artifacts[3]["captured_at"] = "2026-06-28T00:07:00Z"
    artifacts[4]["captured_at"] = "2026-06-28T00:09:00Z"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].captured_at_must_be_after_broker_order_receipt_event" in reason
    assert "evidence_artifacts[1].captured_at_must_be_after_provider_status_export_event" in reason
    assert "evidence_artifacts[2].captured_at_must_be_after_cancel_confirmation_event" in reason
    assert "evidence_artifacts[3].captured_at_must_be_after_unknown_recovery_review_event" in reason
    assert "evidence_artifacts[4].captured_at_must_be_after_repository_audit_export_event" in reason
    assert "2026-06-28T00" not in reason


def test_provider_lifecycle_evidence_rejects_cancel_artifact_before_canceled_pair() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[2]["captured_at"] = "2026-06-28T00:04:15Z"

    with pytest.raises(
        EvidenceValidationError,
        match=r"evidence_artifacts\[2\]\.captured_at_must_be_after_cancel_confirmation_event",
    ):
        verify_provider_lifecycle_evidence(evidence)


def test_provider_lifecycle_evidence_rejects_missing_or_weak_artifacts() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts.pop()
    artifacts[0]["uri"] = "file:///tmp/sample-provider-lifecycle.json?token=abc"
    artifacts[0]["sha256"] = "not-a-sha"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts.missing_repository_audit_export" in reason
    assert "evidence_artifacts[0].uri_must_not_include_query_or_fragment" in reason
    assert "evidence_artifacts[0].uri_must_not_be_mock_fixture_or_local" in reason
    assert "evidence_artifacts[0].sha256_must_be_64_hex" in reason
    assert "token=abc" not in reason


def test_provider_lifecycle_evidence_rejects_non_https_artifact_uri() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = "ops://provider-lifecycle/toss-sandbox-2026-06-28/order-receipt"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_must_be_https_uri" in reason
    assert "provider-lifecycle/toss-sandbox" not in reason


def test_provider_lifecycle_evidence_rejects_malformed_artifact_uri_port() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = (
        "https://evidence.kr-autotrading.net:bad/provider-lifecycle/"
        "toss-sandbox-2026-06-28/order-receipt"
    )

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_must_have_valid_port" in reason
    assert "bad" not in reason


def test_provider_lifecycle_evidence_rejects_artifact_uri_path_traversal() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = (
        "https://evidence.kr-autotrading.net/provider-lifecycle/"
        "toss-sandbox-2026-06-28/%252e%252e/order-receipt"
    )

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_must_not_include_path_traversal" in reason
    assert "%252e%252e" not in reason


def test_provider_lifecycle_evidence_rejects_local_https_artifact_host() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = "https://10.0.0.5/provider-lifecycle/toss-sandbox/order-receipt"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_must_be_remote_retained_uri" in reason
    assert "10.0.0.5" not in reason


def test_provider_lifecycle_evidence_rejects_non_global_https_artifact_ip_host() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = "https://100.64.0.5/provider-lifecycle/toss-sandbox/order-receipt"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_must_be_remote_retained_uri" in reason
    assert "100.64.0.5" not in reason


def test_provider_lifecycle_evidence_rejects_private_dns_artifact_host() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = "https://ops.internal/provider-lifecycle/toss-sandbox/order-receipt"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_must_be_remote_retained_uri" in reason
    assert "ops.internal" not in reason


def test_provider_lifecycle_evidence_rejects_invalid_dns_artifact_host() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = "https://ops_.internal/provider-lifecycle/toss-sandbox/order-receipt"

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_must_be_remote_retained_uri" in reason
    assert "ops_.internal" not in reason


def test_provider_lifecycle_evidence_rejects_unbound_artifacts() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[1]["drill_id"] = "different-drill"
    artifacts[2]["uri"] = artifacts[0]["uri"]
    artifacts[2]["sha256"] = artifacts[0]["sha256"]

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[1].drill_id_must_match_evidence_drill_id" in reason
    assert "evidence_artifacts[2].uri_duplicate" in reason
    assert "evidence_artifacts[2].sha256_duplicate" in reason


def test_provider_lifecycle_evidence_rejects_case_variant_artifact_sha_reuse() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[2]["sha256"] = str(artifacts[0]["sha256"]).upper()

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[2].sha256_duplicate" in reason
    assert str(artifacts[2]["sha256"]) not in reason


def test_provider_lifecycle_evidence_rejects_canonical_artifact_uri_reuse() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[2]["uri"] = (
        "https://EVIDENCE.KR-AUTOTRADING.NET:443/provider-lifecycle/"
        "toss-sandbox-2026-06-28/order-receipt"
    )

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[2].uri_duplicate" in reason
    assert "EVIDENCE.KR-AUTOTRADING.NET" not in reason


def test_provider_lifecycle_evidence_rejects_percent_encoded_artifact_uri_reuse() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[2]["uri"] = (
        "https://evidence.kr-autotrading.net/provider-lifecycle/"
        "toss-sandbox-2026-06-28/%6frder-receipt"
    )

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_evidence(evidence)

    reason = str(exc_info.value)
    assert "evidence_artifacts[2].uri_duplicate" in reason
    assert "%6frder-receipt" not in reason


def test_provider_lifecycle_evidence_file_verifies_remote_artifacts(
    tmp_path: Path,
) -> None:
    evidence = _valid_evidence()
    bodies = _attach_remote_artifact_hashes(evidence)
    evidence_path = _write_evidence(tmp_path, evidence)
    calls: list[tuple[str, int]] = []

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        calls.append((uri, timeout_seconds))
        return bodies[uri]

    summary = verify_provider_lifecycle_evidence_file(
        evidence_path,
        verify_remote_artifacts=True,
        remote_fetcher=fetcher,
        remote_timeout_seconds=3,
    )

    assert summary.evidence_artifacts == 5
    assert len(calls) == 5
    assert {timeout for _, timeout in calls} == {3}


def test_provider_lifecycle_evidence_remote_artifacts_cli_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _valid_evidence()
    bodies = _attach_remote_artifact_hashes(evidence)
    evidence_path = _write_evidence(tmp_path, evidence)

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        assert timeout_seconds == 10
        return bodies[uri]

    monkeypatch.setattr(
        "app.tools.verify_provider_lifecycle_evidence._default_remote_artifact_fetcher",
        fetcher,
    )

    exit_code = main(["--evidence", str(evidence_path), "--verify-remote-artifacts"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS provider_lifecycle_evidence" in output


def test_provider_lifecycle_remote_artifacts_rejects_sha_mismatch_without_leak() -> None:
    evidence = _valid_evidence()
    bodies = _attach_remote_artifact_hashes(evidence)
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["sha256"] = _real_sha256(b"different-provider-artifact")

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        return bodies[uri]

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_remote_artifacts(evidence, fetcher=fetcher)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_remote_sha256_mismatch" in reason
    assert "provider-artifact-0" not in reason
    assert "order-receipt" not in reason


def test_provider_lifecycle_remote_artifacts_rejects_fetch_failure_without_leak() -> None:
    evidence = _valid_evidence()
    _attach_remote_artifact_hashes(evidence)

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        raise OSError("secret provider evidence token")

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_remote_artifacts(evidence, fetcher=fetcher)

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_remote_fetch_failed" in reason
    assert "secret provider evidence token" not in reason
    assert "evidence.kr-autotrading.net" not in reason


def test_provider_lifecycle_remote_artifacts_rejects_github_blob_page() -> None:
    evidence = _valid_evidence()
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    artifacts[0]["uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/provider-lifecycle/"
        "toss-sandbox-2026-06-28/order-receipt"
    )

    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_provider_lifecycle_remote_artifacts(
            evidence,
            fetcher=lambda uri, timeout_seconds: b"unused",
        )

    reason = str(exc_info.value)
    assert "evidence_artifacts[0].uri_remote_must_reference_raw_artifact_bytes" in reason
    assert "github.com" not in reason
    assert "order-receipt" not in reason


def _write_evidence(tmp_path: Path, evidence: dict[str, object]) -> Path:
    path = tmp_path / "provider_lifecycle_evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _valid_evidence() -> dict[str, object]:
    return copy.deepcopy(
        {
            "schema_version": 1,
            "drill_id": "toss-sandbox-2026-06-28",
            "provider": "toss",
            "environment": "sandbox",
            "started_at": "2026-06-28T00:00:00Z",
            "completed_at": "2026-06-28T00:10:00Z",
            "live_order_allowed_before": False,
            "live_order_allowed_after": False,
            "created_order": {
                "local_order_id": "11111111-1111-4111-8111-111111111111",
                "created_at": "2026-06-28T00:00:30Z",
                "symbol": "005930",
                "action": "buy",
                "order_type": "KRX_LIMIT",
                "provider_order_id_redacted": "toss_order_...abcd",
                "amount_krw": 75000,
                "status_after_create": "sent",
            },
            "provider_status_sequence": [
                {
                    "observed_at": "2026-06-28T00:01:00Z",
                    "local_order_id": "11111111-1111-4111-8111-111111111111",
                    "provider_status": "PENDING",
                    "local_status": "sent",
                },
                {
                    "observed_at": "2026-06-28T00:04:30Z",
                    "local_order_id": "11111111-1111-4111-8111-111111111111",
                    "provider_status": "CANCELED",
                    "local_status": "canceled",
                },
            ],
            "cancel_probe": {
                "attempted": True,
                "attempted_at": "2026-06-28T00:04:00Z",
                "local_order_id": "11111111-1111-4111-8111-111111111111",
                "provider_cancel_id_redacted": "toss_cancel_...dcba",
                "provider_final_status": "CANCELED",
                "local_status": "canceled",
            },
            "unknown_recovery": {
                "order_id": "11111111-1111-4111-8111-111111111111",
                "reason": "provider_timeout",
                "engine_event_message": "live_order_manual_check_still_unknown",
                "operator_reviewed_at": "2026-06-28T00:07:00Z",
                "operator_reviewed_by": "ops-admin-1",
                "final_status": "canceled",
            },
            "audit": {
                "orders_reviewed": 3,
                "engine_events_reviewed": 2,
                "audit_logs_reviewed": 2,
                "reviewed_by": "ops-admin-2",
                "reviewed_at": "2026-06-28T00:09:00Z",
            },
            "evidence_artifacts": [
                {
                    "type": "broker_order_receipt",
                    "drill_id": "toss-sandbox-2026-06-28",
                    "uri": (
                        "https://evidence.kr-autotrading.net/provider-lifecycle/"
                        "toss-sandbox-2026-06-28/order-receipt"
                    ),
                    "sha256": _sha256("a"),
                    "captured_at": "2026-06-28T00:02:00Z",
                },
                {
                    "type": "provider_status_export",
                    "drill_id": "toss-sandbox-2026-06-28",
                    "uri": (
                        "https://evidence.kr-autotrading.net/provider-lifecycle/"
                        "toss-sandbox-2026-06-28/status-export"
                    ),
                    "sha256": _sha256("b"),
                    "captured_at": "2026-06-28T00:05:00Z",
                },
                {
                    "type": "cancel_confirmation",
                    "drill_id": "toss-sandbox-2026-06-28",
                    "uri": (
                        "https://evidence.kr-autotrading.net/provider-lifecycle/"
                        "toss-sandbox-2026-06-28/cancel-confirmation"
                    ),
                    "sha256": _sha256("c"),
                    "captured_at": "2026-06-28T00:05:00Z",
                },
                {
                    "type": "unknown_recovery_review",
                    "drill_id": "toss-sandbox-2026-06-28",
                    "uri": (
                        "https://evidence.kr-autotrading.net/provider-lifecycle/"
                        "toss-sandbox-2026-06-28/unknown-review"
                    ),
                    "sha256": _sha256("d"),
                    "captured_at": "2026-06-28T00:07:30Z",
                },
                {
                    "type": "repository_audit_export",
                    "drill_id": "toss-sandbox-2026-06-28",
                    "uri": (
                        "https://evidence.kr-autotrading.net/provider-lifecycle/"
                        "toss-sandbox-2026-06-28/repository-audit"
                    ),
                    "sha256": _sha256("e"),
                    "captured_at": "2026-06-28T00:09:30Z",
                },
            ],
        }
    )


def _sha256(prefix: str) -> str:
    return (prefix * 64)[:64]


def _real_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _attach_remote_artifact_hashes(evidence: dict[str, object]) -> dict[str, bytes]:
    bodies: dict[str, bytes] = {}
    artifacts = cast(list[dict[str, object]], evidence["evidence_artifacts"])
    for index, artifact in enumerate(artifacts):
        uri = cast(str, artifact["uri"])
        body = f"provider-artifact-{index}".encode()
        artifact["sha256"] = _real_sha256(body)
        bodies[uri] = body
    return bodies

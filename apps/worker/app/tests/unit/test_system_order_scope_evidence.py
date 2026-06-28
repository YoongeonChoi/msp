from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.tools.verify_system_order_scope_evidence import (
    SystemOrderScopeEvidenceValidationError,
    main,
    verify_system_order_scope_evidence,
    verify_system_order_scope_evidence_file,
)


def test_system_order_scope_evidence_passes_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = tmp_path / "system-order-scope-evidence.json"
    evidence_path.write_text(json.dumps(_valid_evidence()), encoding="utf-8")

    exit_code = main(["--evidence", str(evidence_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "FINAL=PASS system_order_scope_evidence "
        "scope=system_created_live_orders_only broker=toss "
        "deployment_environment=staging accepted_by=ops-admin-1"
    ) in output


def test_system_order_scope_evidence_file_verifies_remote_evidence(
    tmp_path: Path,
) -> None:
    body = b"system-order-scope-approval"
    evidence = _valid_evidence()
    evidence["evidence_sha256"] = _real_sha256(body)
    evidence_path = tmp_path / "system-order-scope-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    calls: list[tuple[str, int]] = []

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        calls.append((uri, timeout_seconds))
        return body

    summary = verify_system_order_scope_evidence_file(
        evidence_path,
        verify_remote_evidence=True,
        remote_fetcher=fetcher,
        remote_timeout_seconds=4,
    )

    assert summary.scope == "system_created_live_orders_only"
    assert calls == [(str(evidence["evidence_uri"]), 4)]


def test_system_order_scope_evidence_cli_verifies_remote_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"system-order-scope-cli-approval"
    evidence = _valid_evidence()
    evidence["evidence_sha256"] = _real_sha256(body)
    evidence_path = tmp_path / "system-order-scope-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        assert timeout_seconds == 10
        assert uri == evidence["evidence_uri"]
        return body

    monkeypatch.setattr(
        "app.tools.verify_live_readiness_evidence_bundle._default_remote_evidence_fetcher",
        fetcher,
    )

    exit_code = main(["--evidence", str(evidence_path), "--verify-remote-evidence"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS system_order_scope_evidence" in output


def test_system_order_scope_remote_evidence_rejects_sha_mismatch_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["evidence_sha256"] = _real_sha256(b"different-scope-approval")

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(
            evidence,
            verify_remote_evidence=True,
            remote_fetcher=lambda uri, timeout_seconds: b"system-order-scope-approval",
        )

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_remote_sha256_mismatch" in reason
    assert "system-order-scope-approval" not in reason
    assert "SCOPE-20260628-1" not in reason


def test_system_order_scope_remote_evidence_rejects_fetch_failure_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["evidence_sha256"] = _real_sha256(b"system-order-scope-approval")

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        raise OSError("secret scope evidence token")

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(
            evidence,
            verify_remote_evidence=True,
            remote_fetcher=fetcher,
        )

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_remote_fetch_failed" in reason
    assert "secret scope evidence token" not in reason
    assert "evidence.kr-autotrading.net" not in reason


def test_system_order_scope_remote_evidence_rejects_github_blob_page() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = (
        "https://github.com/YoongeonChoi/msp/blob/main/approvals/SCOPE-20260628-1"
    )

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(
            evidence,
            verify_remote_evidence=True,
            remote_fetcher=lambda uri, timeout_seconds: b"unused",
        )

    reason = str(exc_info.value)
    assert (
        "system_order_scope_evidence."
        "evidence_uri_remote_must_reference_raw_artifact_bytes"
    ) in reason
    assert "github.com" not in reason
    assert "SCOPE-20260628-1" not in reason


def test_system_order_scope_evidence_rejects_weak_uri_without_leaking_query() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "https://example.test/mock-scope?token=secret-token"
    evidence["evidence_sha256"] = "not-a-sha"
    evidence["runtime_env_value_confirmed"] = False

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_must_not_be_mock_or_fixture" in reason
    assert "system_order_scope_evidence.evidence_uri_must_not_include_query_or_fragment" in reason
    assert "system_order_scope_evidence.evidence_sha256_must_be_64_hex" in reason
    assert "system_order_scope_evidence.runtime_env_value_confirmed_must_be_true" in reason
    assert "token=secret-token" not in reason


def test_system_order_scope_evidence_rejects_local_file_uri() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "file://C:/tmp/system-order-scope-evidence.json"

    with pytest.raises(
        SystemOrderScopeEvidenceValidationError,
        match="system_order_scope_evidence.evidence_uri_must_not_be_mock_or_fixture",
    ):
        verify_system_order_scope_evidence(evidence)


def test_system_order_scope_evidence_rejects_non_https_uri() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "s3://ops-internal/approvals/SCOPE-20260628-1"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_must_be_https_uri" in reason
    assert "ops-internal/approvals" not in reason


def test_system_order_scope_evidence_rejects_local_https_evidence_host() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "https://10.0.0.5/approvals/SCOPE-20260628-1"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_must_be_remote_retained_uri" in reason
    assert "10.0.0.5" not in reason


def test_system_order_scope_evidence_rejects_non_global_https_evidence_ip_host() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "https://100.64.0.5/approvals/SCOPE-20260628-1"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_must_be_remote_retained_uri" in reason
    assert "100.64.0.5" not in reason


def test_system_order_scope_evidence_rejects_private_dns_evidence_host() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "https://ops.internal/approvals/SCOPE-20260628-1"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_must_be_remote_retained_uri" in reason
    assert "ops.internal" not in reason


def test_system_order_scope_evidence_rejects_invalid_dns_evidence_host() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "https://ops_.internal/approvals/SCOPE-20260628-1"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_uri_must_be_remote_retained_uri" in reason
    assert "ops_.internal" not in reason


def test_system_order_scope_evidence_rejects_missing_retained_uri() -> None:
    evidence = _valid_evidence()
    evidence["evidence_uri"] = "ops-internal-approval-SCOPE-20260628-1"

    with pytest.raises(
        SystemOrderScopeEvidenceValidationError,
        match="system_order_scope_evidence.evidence_uri_must_be_retained_uri",
    ):
        verify_system_order_scope_evidence(evidence)


def test_system_order_scope_evidence_rejects_future_acceptance_time() -> None:
    evidence = _valid_evidence()
    evidence["accepted_at"] = "2099-06-28T01:09:00Z"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.accepted_at_must_not_be_future" in reason
    assert "2099" not in reason


def test_system_order_scope_evidence_requires_evidence_capture_time() -> None:
    evidence = _valid_evidence()
    del evidence["evidence_captured_at"]

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    assert (
        "system_order_scope_evidence.evidence_captured_at_must_be_non_empty_string"
        in str(exc_info.value)
    )


def test_system_order_scope_evidence_rejects_future_evidence_capture_time() -> None:
    evidence = _valid_evidence()
    evidence["evidence_captured_at"] = "2099-06-28T01:10:00Z"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.evidence_captured_at_must_not_be_future" in reason
    assert "2099" not in reason


def test_system_order_scope_evidence_rejects_capture_not_after_acceptance() -> None:
    evidence = _valid_evidence()
    evidence["accepted_at"] = "2026-06-28T01:09:00Z"
    evidence["evidence_captured_at"] = "2026-06-28T01:09:00Z"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert (
        "system_order_scope_evidence.evidence_captured_at_must_be_after_accepted_at"
        in reason
    )
    assert "2026-06-28T01" not in reason


def test_system_order_scope_evidence_rejects_sensitive_unknown_key_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["operator_jwt"] = "scope-secret-that-must-not-print"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "sensitive_key_not_allowed:system_order_scope_evidence.operator_jwt" in reason
    assert "system_order_scope_evidence.unknown_keys=operator_jwt" in reason
    assert "scope-secret-that-must-not-print" not in reason


def test_system_order_scope_evidence_rejects_automated_acceptance_operator() -> None:
    evidence = _valid_evidence()
    evidence["accepted_by"] = "scope-automation-bot"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.accepted_by_must_be_human" in reason
    assert "scope-automation-bot" not in reason


def test_system_order_scope_evidence_rejects_email_like_acceptance_operator_without_leak() -> None:
    evidence = _valid_evidence()
    evidence["accepted_by"] = "ops-admin@example.com"

    with pytest.raises(SystemOrderScopeEvidenceValidationError) as exc_info:
        verify_system_order_scope_evidence(evidence)

    reason = str(exc_info.value)
    assert "system_order_scope_evidence.accepted_by_must_be_logical_operator_id" in reason
    assert "ops-admin@example.com" not in reason


def test_system_order_scope_evidence_accepts_human_operator_with_ci_substring() -> None:
    evidence = _valid_evidence()
    evidence["accepted_by"] = "alice.operator"

    summary = verify_system_order_scope_evidence(evidence)

    assert summary.accepted_by == "alice.operator"


def _valid_evidence() -> dict[str, object]:
    return copy.deepcopy(
        {
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
            "evidence_sha256": ("abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"),
        }
    )


def _real_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()

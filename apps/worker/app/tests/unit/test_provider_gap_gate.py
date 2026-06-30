import hashlib
import json
from pathlib import Path

import pytest

from app.application.services.provider_gap_gate import (
    evaluate_provider_api_gaps,
    format_provider_gap_gate_final_line,
    format_provider_gap_gate_report,
    provider_api_gap_id,
    verify_provider_gap_evidence,
)
from app.tools import check_provider_contract_gaps


def test_provider_gap_gate_blocks_unknown_statuses() -> None:
    report = evaluate_provider_api_gaps(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order status | verified-readonly-implemented | checked |",
                "| KRX | market calendar | unknown | verify official docs |",
                "| Toss | live account count | "
                "partial-system-only-accepted-fail-closed | fail closed |",
            ]
        )
    )

    assert report.passed is False
    assert len(report.gaps) == 3
    assert [gap.provider for gap in report.blocking_gaps] == ["KRX"]
    assert [gap.provider for gap in report.unknown_gaps] == ["KRX"]
    assert not report.invalid_status_gaps
    assert [gap.provider for gap in report.warning_gaps] == ["Toss"]
    assert (
        "FINAL=FAIL provider_contract_gaps total_gaps=3 "
        "blocking_unknown_gaps=1 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=toss:live-account-count:"
        "partial-system-only-accepted-fail-closed system_order_scope_accepted=0 "
        "provider_gap_evidence=0"
    ) in format_provider_gap_gate_report(report)


def test_provider_gap_gate_passes_for_verified_or_intentionally_blocked_gaps() -> None:
    report = evaluate_provider_api_gaps(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
                "| Toss | order modify | verified-not-implemented | intentionally blocked |",
            ]
        )
    )

    assert report.passed is True
    assert not report.blocking_gaps
    assert (
        "FINAL=PASS provider_contract_gaps total_gaps=2 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=0 "
        "warning_partial_gap_ids=none system_order_scope_accepted=0 "
        "provider_gap_evidence=1"
    ) in format_provider_gap_gate_report(
        report,
        provider_gap_evidence_verified=True,
    )
    assert format_provider_gap_gate_final_line(
        report,
        provider_gap_evidence_verified=True,
    ) == (
        "FINAL=PASS provider_contract_gaps total_gaps=2 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=0 "
        "warning_partial_gap_ids=none system_order_scope_accepted=0 "
        "provider_gap_evidence=1"
    )


def test_provider_gap_gate_blocks_unrecognized_statuses() -> None:
    report = evaluate_provider_api_gaps(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| KRX | listing | verified-readonly-implemeted | typo must block |",
            ]
        )
    )

    assert report.passed is False
    assert [gap.provider for gap in report.blocking_gaps] == ["KRX"]
    assert [gap.status for gap in report.invalid_status_gaps] == [
        "verified-readonly-implemeted"
    ]
    assert (
        "FINAL=FAIL provider_contract_gaps total_gaps=1 "
        "blocking_unknown_gaps=0 invalid_status_gaps=1 warning_partial_gaps=0 "
        "warning_partial_gap_ids=none system_order_scope_accepted=0 "
        "provider_gap_evidence=0"
    ) in format_provider_gap_gate_report(report)


def test_provider_gap_gate_blocks_unrecognized_partial_statuses() -> None:
    report = evaluate_provider_api_gaps(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | live account count | "
                "partial-readonly-local-count-implemented-fail-closed | typo must block |",
            ]
        )
    )

    assert report.passed is False
    assert not report.warning_gaps
    assert [gap.status for gap in report.invalid_status_gaps] == [
        "partial-readonly-local-count-implemented-fail-closed"
    ]
    assert (
        "FINAL=FAIL provider_contract_gaps total_gaps=1 "
        "blocking_unknown_gaps=0 invalid_status_gaps=1 warning_partial_gaps=0 "
        "warning_partial_gap_ids=none system_order_scope_accepted=0 "
        "provider_gap_evidence=0"
    ) in format_provider_gap_gate_report(report)


def test_provider_gap_gate_cli_outputs_single_final_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
                "| Toss | live account count | "
                "partial-system-only-accepted-fail-closed | fail closed |",
            ]
        ),
        encoding="utf-8",
    )
    provider_gap_evidence = tmp_path / "invalid-provider-gap-evidence.json"
    provider_gap_evidence.write_text("{", encoding="utf-8")
    monkeypatch.setattr(check_provider_contract_gaps, "API_GAPS", api_gaps)

    with pytest.raises(SystemExit) as exc_info:
        check_provider_contract_gaps.main(
            ["--provider-gap-evidence", str(provider_gap_evidence)]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert stdout_lines == [
        "FINAL=FAIL provider_contract_gaps total_gaps=2 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=toss:live-account-count:"
        "partial-system-only-accepted-fail-closed system_order_scope_accepted=0 "
        "provider_gap_evidence=0"
    ]
    assert captured.err == ""


def test_provider_gap_gate_cli_accepts_warning_with_system_order_scope_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
                "| Toss | live account count | "
                "partial-system-only-accepted-fail-closed | fail closed |",
            ]
        ),
        encoding="utf-8",
    )
    scope_evidence = _write_system_order_scope_evidence(tmp_path)
    provider_gap_evidence = _write_provider_gap_evidence(tmp_path, api_gaps)
    monkeypatch.setattr(check_provider_contract_gaps, "API_GAPS", api_gaps)

    with pytest.raises(SystemExit) as exc_info:
        check_provider_contract_gaps.main(
            [
                "--system-order-scope-evidence",
                str(scope_evidence),
                "--provider-gap-evidence",
                str(provider_gap_evidence),
            ]
        )

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert stdout_lines == [
        "FINAL=PASS provider_contract_gaps total_gaps=2 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 "
        "warning_partial_gap_ids=toss:live-account-count:"
        "partial-system-only-accepted-fail-closed system_order_scope_accepted=1 "
        "provider_gap_evidence=1"
    ]
    assert captured.err == ""


def test_provider_gap_gate_cli_remote_flag_outputs_single_final_line_on_raw_requirement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
                "| Toss | order modify | verified-not-implemented | intentionally blocked |",
            ]
        ),
        encoding="utf-8",
    )
    provider_gap_evidence = _write_provider_gap_evidence(tmp_path, api_gaps)
    evidence = json.loads(provider_gap_evidence.read_text(encoding="utf-8"))
    evidence["source_artifacts"][0]["artifact_uri"] = (
        "https://github.com/example-org/example-repo/blob/main/toss-openapi.json"
    )
    provider_gap_evidence.write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setattr(check_provider_contract_gaps, "API_GAPS", api_gaps)

    with pytest.raises(SystemExit) as exc_info:
        check_provider_contract_gaps.main(
            [
                "--provider-gap-evidence",
                str(provider_gap_evidence),
                "--verify-remote-provider-gap-artifacts",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert stdout_lines == [
        "FINAL=FAIL provider_contract_gaps total_gaps=2 "
        "blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=0 "
        "warning_partial_gap_ids=none system_order_scope_accepted=0 "
        "provider_gap_evidence=0"
    ]
    assert captured.err == ""


def test_provider_gap_evidence_binds_api_gaps_hash_and_all_gap_ids(tmp_path: Path) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
                "| Toss | order modify | verified-not-implemented | intentionally blocked |",
            ]
        ),
        encoding="utf-8",
    )
    evidence = json.loads(_write_provider_gap_evidence(tmp_path, api_gaps).read_text())

    summary = verify_provider_gap_evidence(
        api_gaps.read_text(encoding="utf-8"),
        evidence,
    )

    assert summary.gap_count == 2
    assert summary.source_artifacts == 1


def test_provider_gap_evidence_verifies_remote_artifact_bytes(tmp_path: Path) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
                "| Toss | order modify | verified-not-implemented | intentionally blocked |",
            ]
        ),
        encoding="utf-8",
    )
    evidence = json.loads(_write_provider_gap_evidence(tmp_path, api_gaps).read_text())
    artifact_body = b'{"openapi":"3.0.0","paths":{}}\n'
    artifact_uri = evidence["source_artifacts"][0]["artifact_uri"]
    evidence["source_artifacts"][0]["artifact_sha256"] = hashlib.sha256(
        artifact_body
    ).hexdigest()
    calls: list[tuple[str, int]] = []

    def fetcher(uri: str, timeout_seconds: int) -> bytes:
        calls.append((uri, timeout_seconds))
        return artifact_body

    summary = verify_provider_gap_evidence(
        api_gaps.read_text(encoding="utf-8"),
        evidence,
        verify_remote_artifacts=True,
        remote_fetcher=fetcher,
        remote_timeout_seconds=3,
    )

    assert summary.gap_count == 2
    assert summary.source_artifacts == 1
    assert calls == [(artifact_uri, 3)]


def test_provider_gap_evidence_rejects_remote_artifact_hash_mismatch_safely(
    tmp_path: Path,
) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
            ]
        ),
        encoding="utf-8",
    )
    evidence = json.loads(_write_provider_gap_evidence(tmp_path, api_gaps).read_text())
    artifact_uri = evidence["source_artifacts"][0]["artifact_uri"]
    artifact_sha256 = evidence["source_artifacts"][0]["artifact_sha256"]

    with pytest.raises(ValueError) as exc_info:
        verify_provider_gap_evidence(
            api_gaps.read_text(encoding="utf-8"),
            evidence,
            verify_remote_artifacts=True,
            remote_fetcher=lambda _uri, _timeout: b"published-provider-gap-bytes",
        )

    reason = str(exc_info.value)
    assert (
        "provider_gap_evidence.source_artifacts[0]."
        "artifact_uri_remote_sha256_mismatch"
    ) in reason
    assert artifact_uri not in reason
    assert artifact_sha256 not in reason
    assert "published-provider-gap-bytes" not in reason


def test_provider_gap_evidence_rejects_github_blob_artifact_uri_before_fetch(
    tmp_path: Path,
) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
            ]
        ),
        encoding="utf-8",
    )
    evidence = json.loads(_write_provider_gap_evidence(tmp_path, api_gaps).read_text())
    evidence["source_artifacts"][0]["artifact_uri"] = (
        "https://github.com/example-org/example-repo/blob/main/toss-openapi.json"
    )
    calls = 0

    def fetcher(_uri: str, _timeout_seconds: int) -> bytes:
        nonlocal calls
        calls += 1
        return b"unused"

    with pytest.raises(ValueError) as exc_info:
        verify_provider_gap_evidence(
            api_gaps.read_text(encoding="utf-8"),
            evidence,
            verify_remote_artifacts=True,
            remote_fetcher=fetcher,
        )

    assert (
        "provider_gap_evidence.source_artifacts[0]."
        "artifact_uri_remote_must_reference_raw_artifact_bytes"
    ) in str(exc_info.value)
    assert calls == 0


def test_provider_gap_evidence_rejects_api_gaps_hash_mismatch(
    tmp_path: Path,
) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
            ]
        ),
        encoding="utf-8",
    )
    evidence = json.loads(_write_provider_gap_evidence(tmp_path, api_gaps).read_text())
    evidence["api_gaps_sha256"] = "0" * 64

    with pytest.raises(ValueError) as exc_info:
        verify_provider_gap_evidence(api_gaps.read_text(encoding="utf-8"), evidence)

    assert "provider_gap_evidence.api_gaps_sha256_must_match_api_gaps" in str(
        exc_info.value
    )


def test_provider_gap_evidence_rejects_missing_gap_coverage(tmp_path: Path) -> None:
    api_gaps = tmp_path / "API_GAPS.md"
    api_gaps.write_text(
        "\n".join(
            [
                "| Provider | Gap | Status | Exact verification step |",
                "| --- | --- | --- | --- |",
                "| Toss | order create | verified-limit-implemented | checked |",
                "| Toss | order modify | verified-not-implemented | intentionally blocked |",
            ]
        ),
        encoding="utf-8",
    )
    evidence = json.loads(_write_provider_gap_evidence(tmp_path, api_gaps).read_text())
    evidence["source_artifacts"][0]["gap_ids"] = evidence["gap_ids"][:1]

    with pytest.raises(ValueError) as exc_info:
        verify_provider_gap_evidence(api_gaps.read_text(encoding="utf-8"), evidence)

    assert "provider_gap_evidence.source_artifacts_must_cover_every_gap" in str(
        exc_info.value
    )


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
                "evidence_uri": (
                    "https://evidence.kr-autotrading.net/approvals/SCOPE-20260628-1"
                ),
                "evidence_sha256": (
                    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
                ),
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_provider_gap_evidence(tmp_path: Path, api_gaps: Path) -> Path:
    api_gaps_markdown = api_gaps.read_text(encoding="utf-8")
    report = evaluate_provider_api_gaps(api_gaps_markdown)
    gap_ids = [provider_api_gap_id(gap) for gap in report.gaps]
    path = tmp_path / "provider-gap-evidence.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "api_gaps_sha256": hashlib.sha256(
                    api_gaps_markdown.encode("utf-8")
                ).hexdigest(),
                "gap_ids": gap_ids,
                "captured_at": "2025-01-02T00:00:00Z",
                "source_artifacts": [
                    {
                        "provider": "Toss",
                        "source_name": "toss-openapi-json",
                        "gap_ids": gap_ids,
                        "artifact_uri": (
                            "https://evidence.kr-autotrading.net/provider-gaps/"
                            "toss-openapi.json"
                        ),
                        "artifact_sha256": (
                            "0123456789abcdef0123456789abcdef"
                            "0123456789abcdef0123456789abcdef"
                        ),
                        "captured_at": "2025-01-02T00:00:01Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path

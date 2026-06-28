from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from app.tools.verify_live_readiness_evidence_bundle import (
    REMOTE_EVIDENCE_TIMEOUT_SECONDS,
    BundleValidationError,
    IncidentResponseEvidenceSummary,
    verify_incident_response_channel_remote_evidence,
    verify_incident_response_evidence_parts,
)
from app.tools.verify_provider_lifecycle_evidence import RemoteArtifactFetcher

ACK_GATED_INCIDENT_PASS_PREFIX = "FINAL=PASS live_incident_response_drill"


class IncidentResponseEvidenceValidationError(ValueError):
    pass


def verify_incident_response_evidence_files(
    *,
    incident_output_file: Path,
    incident_channel_evidence: Path,
    verify_remote_channel_evidence: bool = False,
    remote_fetcher: RemoteArtifactFetcher | None = None,
    remote_timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
) -> IncidentResponseEvidenceSummary:
    try:
        incident_output = incident_output_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise IncidentResponseEvidenceValidationError("incident_output_file_unreadable") from exc

    try:
        payload = json.loads(incident_channel_evidence.read_text(encoding="utf-8"))
    except OSError as exc:
        raise IncidentResponseEvidenceValidationError(
            "incident_channel_evidence_unreadable"
        ) from exc
    except json.JSONDecodeError as exc:
        raise IncidentResponseEvidenceValidationError(
            "incident_channel_evidence_json_invalid"
        ) from exc
    if not isinstance(payload, Mapping):
        raise IncidentResponseEvidenceValidationError(
            "incident_channel_evidence_must_be_object"
        )
    return verify_incident_response_evidence(
        incident_output=incident_output,
        incident_channel_evidence=cast(Mapping[str, object], payload),
        verify_remote_channel_evidence=verify_remote_channel_evidence,
        remote_fetcher=remote_fetcher,
        remote_timeout_seconds=remote_timeout_seconds,
    )


def verify_incident_response_evidence(
    *,
    incident_output: str,
    incident_channel_evidence: Mapping[str, object],
    verify_remote_channel_evidence: bool = False,
    remote_fetcher: RemoteArtifactFetcher | None = None,
    remote_timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
) -> IncidentResponseEvidenceSummary:
    final_output = _extract_single_final_line(incident_output)
    if not _final_output_has_required_prefix(
        final_output,
        ACK_GATED_INCIDENT_PASS_PREFIX,
    ):
        raise IncidentResponseEvidenceValidationError(
            "incident_output_final_line_must_be_ack_gated_pass"
        )
    if "FINAL=SKIP" in final_output or "FINAL=FAIL" in final_output:
        raise IncidentResponseEvidenceValidationError(
            "incident_output_final_line_must_not_be_skip_or_fail"
        )
    if "sample" in final_output.lower() or "fixture" in final_output.lower():
        raise IncidentResponseEvidenceValidationError(
            "incident_output_final_line_must_not_be_sample_or_fixture"
        )

    try:
        summary = verify_incident_response_evidence_parts(
            final_output=final_output,
            channel_evidence=incident_channel_evidence,
        )
    except BundleValidationError as exc:
        raise IncidentResponseEvidenceValidationError(str(exc)) from exc
    if verify_remote_channel_evidence:
        try:
            verify_incident_response_channel_remote_evidence(
                incident_channel_evidence,
                fetcher=remote_fetcher,
                timeout_seconds=remote_timeout_seconds,
            )
        except BundleValidationError as exc:
            raise IncidentResponseEvidenceValidationError(str(exc)) from exc
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate retained ACK-gated incident response evidence."
    )
    parser.add_argument(
        "--incident-output-file",
        required=True,
        type=Path,
        help="Path to captured run_live_incident_response_drill_once output.",
    )
    parser.add_argument(
        "--incident-channel-evidence",
        required=True,
        type=Path,
        help="Path to retained incident-channel evidence JSON.",
    )
    parser.add_argument(
        "--verify-remote-channel-evidence",
        action="store_true",
        help=(
            "Fetch retained channel_evidence.evidence_uri over HTTPS and require "
            "downloaded bytes to match channel_evidence.evidence_sha256. Use this "
            "after publishing incident-channel evidence."
        ),
    )
    args = parser.parse_args(argv)

    try:
        summary = verify_incident_response_evidence_files(
            incident_output_file=args.incident_output_file,
            incident_channel_evidence=args.incident_channel_evidence,
            verify_remote_channel_evidence=args.verify_remote_channel_evidence,
        )
    except IncidentResponseEvidenceValidationError as exc:
        print(f"FINAL=FAIL incident_response_evidence reason={_safe_reason(str(exc))}")
        return 1

    print(
        "FINAL=PASS incident_response_evidence "
        f"delivered={summary.delivered} "
        f"max_latency_ms={summary.max_latency_ms} "
        f"ack_latency_ms={summary.ack_latency_ms} "
        f"channel={summary.channel_name} "
        f"operator_ack_by={summary.operator_ack_by}"
    )
    return 0


def _extract_single_final_line(output: str) -> str:
    non_empty_lines = [line.strip() for line in output.splitlines() if line.strip()]
    final_lines = [
        line for line in non_empty_lines if line.startswith("FINAL=")
    ]
    if len(final_lines) != 1:
        raise IncidentResponseEvidenceValidationError(
            f"incident_output_final_line_count={len(final_lines)}"
        )
    if len(non_empty_lines) != 1:
        raise IncidentResponseEvidenceValidationError(
            "incident_output_non_final_lines_not_allowed"
        )
    return final_lines[0]


def _final_output_has_required_prefix(final_output: str, required_prefix: str) -> bool:
    return final_output == required_prefix or final_output.startswith(
        f"{required_prefix} "
    )


def _safe_reason(reason: str) -> str:
    single_line = reason.replace("\r", " ").replace("\n", " ")
    return single_line[:600]


if __name__ == "__main__":
    raise SystemExit(main())

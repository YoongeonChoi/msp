from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from app.tools.verify_live_readiness_evidence_bundle import (
    REMOTE_EVIDENCE_TIMEOUT_SECONDS,
    BundleValidationError,
    SystemOrderScopeEvidenceSummary,
    verify_system_order_scope_evidence_parts,
    verify_system_order_scope_remote_evidence,
)
from app.tools.verify_provider_lifecycle_evidence import RemoteArtifactFetcher

ALLOWED_SYSTEM_ORDER_SCOPE_EVIDENCE_KEYS = {
    "accepted",
    "scope",
    "broker",
    "limitation",
    "runtime_env_var",
    "runtime_env_value_confirmed",
    "deployment_environment",
    "accepted_by",
    "accepted_at",
    "evidence_captured_at",
    "evidence_uri",
    "evidence_sha256",
}


class SystemOrderScopeEvidenceValidationError(ValueError):
    pass


def verify_system_order_scope_evidence_file(
    path: Path,
    *,
    verify_remote_evidence: bool = False,
    remote_fetcher: RemoteArtifactFetcher | None = None,
    remote_timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
) -> SystemOrderScopeEvidenceSummary:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemOrderScopeEvidenceValidationError(
            "system_order_scope_evidence_unreadable"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemOrderScopeEvidenceValidationError(
            "system_order_scope_evidence_json_invalid"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SystemOrderScopeEvidenceValidationError(
            "system_order_scope_evidence_must_be_object"
        )
    return verify_system_order_scope_evidence(
        cast(Mapping[str, object], payload),
        verify_remote_evidence=verify_remote_evidence,
        remote_fetcher=remote_fetcher,
        remote_timeout_seconds=remote_timeout_seconds,
    )


def verify_system_order_scope_evidence(
    evidence: Mapping[str, object],
    *,
    verify_remote_evidence: bool = False,
    remote_fetcher: RemoteArtifactFetcher | None = None,
    remote_timeout_seconds: int = REMOTE_EVIDENCE_TIMEOUT_SECONDS,
) -> SystemOrderScopeEvidenceSummary:
    try:
        summary = verify_system_order_scope_evidence_parts(evidence)
    except BundleValidationError as exc:
        raise SystemOrderScopeEvidenceValidationError(str(exc)) from exc
    if verify_remote_evidence:
        try:
            verify_system_order_scope_remote_evidence(
                evidence,
                fetcher=remote_fetcher,
                timeout_seconds=remote_timeout_seconds,
            )
        except BundleValidationError as exc:
            raise SystemOrderScopeEvidenceValidationError(str(exc)) from exc
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate retained system-order-scope acceptance evidence."
    )
    parser.add_argument(
        "--evidence",
        required=True,
        type=Path,
        help="Path to retained system_order_scope_evidence JSON.",
    )
    parser.add_argument(
        "--verify-remote-evidence",
        action="store_true",
        help=(
            "Fetch retained evidence_uri over HTTPS and require downloaded bytes "
            "to match evidence_sha256. Use this after publishing system-order "
            "scope acceptance evidence."
        ),
    )
    args = parser.parse_args(argv)

    try:
        summary = verify_system_order_scope_evidence_file(
            args.evidence,
            verify_remote_evidence=args.verify_remote_evidence,
        )
    except SystemOrderScopeEvidenceValidationError as exc:
        print(f"FINAL=FAIL system_order_scope_evidence reason={_safe_reason(str(exc))}")
        return 1

    print(
        "FINAL=PASS system_order_scope_evidence "
        f"scope={summary.scope} "
        f"broker={summary.broker} "
        f"deployment_environment={summary.deployment_environment} "
        f"accepted_by={summary.accepted_by}"
    )
    return 0


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:700]


if __name__ == "__main__":
    raise SystemExit(main())

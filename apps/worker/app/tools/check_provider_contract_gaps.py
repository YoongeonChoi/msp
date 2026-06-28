from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from app.application.services.provider_gap_gate import (
    ProviderGapEvidenceValidationError,
    evaluate_provider_api_gaps,
    format_provider_gap_gate_final_line,
    verify_provider_gap_evidence,
)
from app.tools.verify_system_order_scope_evidence import (
    SystemOrderScopeEvidenceValidationError,
    verify_system_order_scope_evidence_file,
)

ROOT = Path(__file__).resolve().parents[4]
API_GAPS = ROOT / "docs" / "API_GAPS.md"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Validate provider API gap status before live readiness claims."
    )
    parser.add_argument(
        "--system-order-scope-evidence",
        type=Path,
        help=(
            "Retained system-order-scope acceptance evidence required when "
            "provider gap warnings remain."
        ),
    )
    parser.add_argument(
        "--provider-gap-evidence",
        type=Path,
        required=True,
        help=(
            "Retained provider API gap evidence manifest binding docs/API_GAPS.md "
            "to official source artifacts."
        ),
    )
    args = parser.parse_args([] if argv is None else argv)

    api_gaps_markdown = API_GAPS.read_text(encoding="utf-8")
    report = evaluate_provider_api_gaps(api_gaps_markdown)
    provider_gap_evidence_verified = False
    try:
        provider_gap_evidence_payload = json.loads(
            args.provider_gap_evidence.read_text(encoding="utf-8")
        )
        if not isinstance(provider_gap_evidence_payload, dict):
            raise ProviderGapEvidenceValidationError(
                "provider_gap_evidence_root_must_be_object"
            )
        verify_provider_gap_evidence(
            api_gaps_markdown,
            cast(dict[str, object], provider_gap_evidence_payload),
        )
    except (OSError, json.JSONDecodeError, ProviderGapEvidenceValidationError):
        provider_gap_evidence_verified = False
    else:
        provider_gap_evidence_verified = True

    system_order_scope_accepted = False
    if args.system_order_scope_evidence is not None:
        try:
            verify_system_order_scope_evidence_file(args.system_order_scope_evidence)
        except SystemOrderScopeEvidenceValidationError:
            system_order_scope_accepted = False
        else:
            system_order_scope_accepted = True
    final_line = format_provider_gap_gate_final_line(
        report,
        system_order_scope_accepted=system_order_scope_accepted,
        provider_gap_evidence_verified=provider_gap_evidence_verified,
    )
    print(final_line)
    raise SystemExit(0 if final_line.startswith("FINAL=PASS ") else 1)


if __name__ == "__main__":
    main(sys.argv[1:])

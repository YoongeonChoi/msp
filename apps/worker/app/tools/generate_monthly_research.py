from __future__ import annotations

import sys

import anyio

from app.tools.generate_monthly_ai_candidate import CliUsageError, _run


def _parse_month(argv: list[str]) -> str:
    if len(argv) != 3 or argv[1] != "--month":
        raise CliUsageError(
            "usage: python -m app.tools.generate_monthly_research --month YYYY-MM"
        )
    month = argv[2]
    if len(month) != 7 or month[4] != "-":
        raise CliUsageError("month must use YYYY-MM format")
    return month


def main() -> None:
    try:
        month = _parse_month(sys.argv)
    except CliUsageError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    candidate = anyio.run(_run, month)
    print(
        "created monthly research candidate "
        f"name={candidate.candidate_name} "
        f"status={candidate.status} "
        f"approval_required={candidate.approval_required}"
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from app.application.use_cases.run_contract_qualification import (
    RunContractQualification,
)


async def _run() -> int:
    started_at = datetime.now(UTC)
    report = await RunContractQualification().execute(
        started_at=started_at,
    )
    print(json.dumps(report.to_json(), ensure_ascii=False, sort_keys=True))
    return 0 if report.result == "pass" else 1


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())

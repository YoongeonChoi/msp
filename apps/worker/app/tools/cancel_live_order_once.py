from __future__ import annotations

import argparse

HISTORICAL_QUARANTINE_REASON = "legacy_live_order_cancel_is_quarantined"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Historical legacy-live cancellation tool; permanently quarantined."
    )
    parser.add_argument("--order-id", required=True)
    parser.parse_args()
    print("FINAL=FAIL")
    print(HISTORICAL_QUARANTINE_REASON)
    raise SystemExit(1)


if __name__ == "__main__":
    main()

import pytest

from app.tools.run_live_recovery_drill_once import (
    HISTORICAL_QUARANTINE_REASON,
    main,
)


async def test_legacy_live_recovery_drill_is_quarantined() -> None:
    with pytest.raises(RuntimeError, match=HISTORICAL_QUARANTINE_REASON):
        await main()

import pytest

from app.tools.run_live_execution_safety_drill_once import (
    HISTORICAL_QUARANTINE_REASON,
    main,
    run_live_execution_safety_drill,
)


async def test_legacy_live_execution_drill_is_quarantined() -> None:
    with pytest.raises(RuntimeError, match=HISTORICAL_QUARANTINE_REASON):
        await run_live_execution_safety_drill()


def test_legacy_live_execution_drill_cli_fails_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "FINAL=FAIL live_execution_safety_drill "
        f"reason={HISTORICAL_QUARANTINE_REASON}"
    ]

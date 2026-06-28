from __future__ import annotations

import pytest

from app.tools.run_live_recovery_drill_once import main


async def test_live_recovery_drill_exercises_reconcile_and_cancel_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await main()

    output = capsys.readouterr().out
    assert "FINAL=PASS live_recovery_drill" in output
    assert "reconciled_updates=1" in output
    assert "manual_check_events=2" in output
    assert "cancel_confirmed=1" in output
    assert "cancel_unknown=1" in output
    assert "pending_order_blocked=1" in output
    assert "manual_check_preserved=1" in output

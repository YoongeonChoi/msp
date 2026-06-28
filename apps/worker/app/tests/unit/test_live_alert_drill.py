from __future__ import annotations

import pytest

from app.tools.run_live_alert_drill_once import main


async def test_live_alert_drill_uses_mock_external_webhook_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

    await main()

    output = capsys.readouterr().out
    assert "FINAL=PASS live_external_alert_drill" in output
    assert "delivered=4" in output

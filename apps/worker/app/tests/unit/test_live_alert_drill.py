from __future__ import annotations

import pytest

from app.tools.run_live_alert_drill_once import main


async def test_live_alert_drill_uses_mock_external_webhook_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_alert_receiver_env(monkeypatch)

    await main()

    output = capsys.readouterr().out
    assert "FINAL=PASS live_external_alert_drill" in output
    assert "delivered=4" in output


def _clear_alert_receiver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALERT_WEBHOOK_URL",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    ):
        monkeypatch.delenv(name, raising=False)

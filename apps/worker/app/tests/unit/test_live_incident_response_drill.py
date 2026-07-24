from __future__ import annotations

import base64
import io
import sys

import pytest

from app.application.ports.alert_port import AlertDeliveryResult
from app.tools.run_live_incident_response_drill_once import (
    IncidentResponseDrillConfigurationError,
    main,
    run_incident_response_drill,
)


async def test_live_incident_delivery_drill_uses_mock_webhook_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_alert_receiver_env(monkeypatch)

    exit_code = await main([])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS live_incident_delivery_drill" in output
    assert "delivered=4" in output
    assert "ack_required=false" in output
    assert "transport=mock" in output


async def test_live_incident_response_drill_refuses_mock_ack_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_alert_receiver_env(monkeypatch)

    exit_code = await main(["--require-ack", "--drill-id", "incident-drill-mock"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert output.strip() == (
        "FINAL=FAIL live_incident_response_drill transport=mock "
        "reason=real_alert_webhook_required_for_ack_drill"
    )


async def test_live_incident_response_drill_requires_exact_operator_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_fake_real_transport(monkeypatch)
    ack_calls: list[tuple[str, float]] = []

    async def ack_reader(drill_id: str, timeout_sec: float) -> bool:
        ack_calls.append((drill_id, timeout_sec))
        return drill_id == "incident-drill-1"

    result = await run_incident_response_drill(
        require_ack=True,
        ack_timeout_sec=1.5,
        ack_reader=ack_reader,
        drill_id="incident-drill-1",
    )

    assert result.delivered == 4
    assert result.ack_required is True
    assert result.acknowledged is True
    assert result.ack_latency_ms is not None
    assert result.transport == "real"
    assert ack_calls == [("incident-drill-1", 1.5)]


async def test_live_incident_response_drill_cli_keeps_ack_prompt_out_of_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_fake_real_transport(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("ACK incident-drill-cli\n"))

    exit_code = await main(
        [
            "--require-ack",
            "--ack-timeout-sec",
            "1.5",
            "--drill-id",
            "incident-drill-cli",
        ]
    )

    captured = capsys.readouterr()
    stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert exit_code == 0
    assert len(stdout_lines) == 1
    assert stdout_lines[0].startswith("FINAL=PASS live_incident_response_drill ")
    assert "delivered=4" in stdout_lines[0]
    assert "max_latency_ms=" in stdout_lines[0]
    assert "acknowledged=true" in stdout_lines[0]
    assert "ack_latency_ms=" in stdout_lines[0]
    assert "drill_id=incident-drill-cli" in stdout_lines[0]
    assert "transport=real" in stdout_lines[0]
    assert "ACK_REQUIRED" not in captured.out
    assert "ACK_REQUIRED type exactly: ACK incident-drill-cli" in captured.err


async def test_live_incident_response_drill_fails_without_operator_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_fake_real_transport(monkeypatch)

    async def ack_reader(drill_id: str, timeout_sec: float) -> bool:
        return False

    result = await run_incident_response_drill(
        require_ack=True,
        ack_timeout_sec=1.5,
        ack_reader=ack_reader,
        drill_id="incident-drill-2",
    )

    assert result.delivered == 4
    assert result.ack_required is True
    assert result.acknowledged is False


async def test_live_incident_response_drill_rejects_unsafe_drill_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_alert_receiver_env(monkeypatch)

    async def ack_reader(drill_id: str, timeout_sec: float) -> bool:
        return False

    with pytest.raises(ValueError, match="invalid_drill_id"):
        await run_incident_response_drill(
            require_ack=True,
            ack_timeout_sec=1.5,
            ack_reader=ack_reader,
            drill_id="bad\nid",
        )


async def test_run_incident_response_drill_refuses_ack_without_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_alert_receiver_env(monkeypatch)

    with pytest.raises(
        IncidentResponseDrillConfigurationError,
        match="real_alert_webhook_required_for_ack_drill",
    ):
        await run_incident_response_drill(require_ack=True, drill_id="incident-drill-3")


@pytest.mark.parametrize("timeout", [0.0, float("nan"), float("inf")])
async def test_incident_drill_rejects_non_positive_or_non_finite_timeout(
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="ack_timeout_sec_must_be_positive"):
        await run_incident_response_drill(
            require_ack=False,
            ack_timeout_sec=timeout,
        )


def _configure_fake_real_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://alerts.example.test/incident")
    monkeypatch.setenv(
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        "incident-drill-current",
    )
    monkeypatch.setenv(
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        base64.b64encode(b"i" * 32).decode("ascii"),
    )
    monkeypatch.delenv("ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64", raising=False)

    async def notify_engine_event(
        self: object,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> AlertDeliveryResult:
        return AlertDeliveryResult(delivered=True, latency_ms=1)

    async def aclose(self: object) -> None:
        return None

    monkeypatch.setattr(
        "app.tools.run_live_incident_response_drill_once.WebhookAlertNotifier.notify_engine_event",
        notify_engine_event,
    )
    monkeypatch.setattr(
        "app.tools.run_live_incident_response_drill_once.WebhookAlertNotifier.aclose",
        aclose,
    )


def _clear_alert_receiver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALERT_WEBHOOK_URL",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    ):
        monkeypatch.delenv(name, raising=False)

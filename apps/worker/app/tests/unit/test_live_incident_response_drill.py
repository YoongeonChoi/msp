from __future__ import annotations

import io
import sys

import pytest

from app.tools.run_live_incident_response_drill_once import (
    main,
    run_incident_response_drill,
)


async def test_live_incident_delivery_drill_uses_mock_webhook_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

    exit_code = await main([])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL=PASS live_incident_delivery_drill" in output
    assert "delivered=4" in output
    assert "ack_required=false" in output


async def test_live_incident_response_drill_requires_exact_operator_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
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
    assert ack_calls == [("incident-drill-1", 1.5)]


async def test_live_incident_response_drill_cli_keeps_ack_prompt_out_of_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
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
    assert "ACK_REQUIRED" not in captured.out
    assert "ACK_REQUIRED type exactly: ACK incident-drill-cli" in captured.err


async def test_live_incident_response_drill_fails_without_operator_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

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
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

    async def ack_reader(drill_id: str, timeout_sec: float) -> bool:
        return False

    with pytest.raises(ValueError, match="invalid_drill_id"):
        await run_incident_response_drill(
            require_ack=True,
            ack_timeout_sec=1.5,
            ack_reader=ack_reader,
            drill_id="bad\nid",
        )

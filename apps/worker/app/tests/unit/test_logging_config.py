from __future__ import annotations

import logging

import pytest

from app.logging_config import configure_logging


def test_http_client_info_logs_cannot_emit_webhook_url_markers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "receiver-query-marker"

    configure_logging()
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info(
            "HTTP Request: POST https://alerts.example.test/path?token=%s",
            marker,
        )
        logging.getLogger("httpcore.connection").info(
            "connect_tcp host=%s",
            marker,
        )

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert marker not in caplog.text

from __future__ import annotations

import base64

import pytest

from app import dead_man_main
from app.dead_man_config import DeadManSettings
from app.infrastructure.authenticated_webhook import ReceiverAckKeyRing


async def test_dead_man_main_wires_only_dead_man_receiver_key_ring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeSource:
        def __init__(self, settings: DeadManSettings) -> None:
            captured["source_settings"] = settings

        async def close(self) -> None:
            captured["source_closed"] = True

    class FakeDestination:
        def __init__(
            self,
            webhook_url: str,
            *,
            key_ring: ReceiverAckKeyRing,
            timeout_sec: float,
        ) -> None:
            captured["webhook_url"] = webhook_url
            captured["key_ids"] = key_ring.accepted_key_ids
            captured["timeout_sec"] = timeout_sec

        async def close(self) -> None:
            captured["destination_closed"] = True

    class FakeLoop:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def run(self) -> None:
            captured["loop_ran"] = True

    monkeypatch.setattr(dead_man_main, "configure_logging", lambda: None)
    monkeypatch.setattr(dead_man_main, "install_signal_handlers", lambda _flag: None)
    monkeypatch.setattr(dead_man_main, "SupabaseDeadManSource", FakeSource)
    monkeypatch.setattr(dead_man_main, "DeadManWebhookDestination", FakeDestination)
    monkeypatch.setattr(dead_man_main, "DeadManMonitorLoop", FakeLoop)
    settings = DeadManSettings.model_validate(
        {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "not-a-" + "production-secret",
            "DEAD_MAN_ACCOUNT_ID": "paper-primary",
            "DEAD_MAN_ALERT_WEBHOOK_URL": ("https://alerts.example.test/dead-man"),
            "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID": ("dead-man-current"),
            "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": (
                base64.b64encode(b"d" * 32).decode("ascii")
            ),
        }
    )

    await dead_man_main.async_main(settings)

    assert captured["key_ids"] == ("dead-man-current",)
    assert captured["webhook_url"] == "https://alerts.example.test/dead-man"
    assert captured["loop_ran"] is True
    assert captured["source_closed"] is True
    assert captured["destination_closed"] is True


async def test_dead_man_main_closes_destination_when_source_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: dict[str, bool] = {}

    class FailingCloseSource:
        def __init__(self, _settings: DeadManSettings) -> None:
            pass

        async def close(self) -> None:
            raise RuntimeError("source_close_failed")

    class RecordingDestination:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def close(self) -> None:
            closed["destination"] = True

    class OneShotLoop:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def run(self) -> None:
            return None

    monkeypatch.setattr(dead_man_main, "configure_logging", lambda: None)
    monkeypatch.setattr(dead_man_main, "install_signal_handlers", lambda _flag: None)
    monkeypatch.setattr(dead_man_main, "SupabaseDeadManSource", FailingCloseSource)
    monkeypatch.setattr(
        dead_man_main,
        "DeadManWebhookDestination",
        RecordingDestination,
    )
    monkeypatch.setattr(dead_man_main, "DeadManMonitorLoop", OneShotLoop)

    with pytest.raises(RuntimeError, match="source_close_failed"):
        await dead_man_main.async_main(_settings())

    assert closed["destination"] is True


def _settings() -> DeadManSettings:
    return DeadManSettings.model_validate(
        {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "not-a-" + "production-secret",
            "DEAD_MAN_ACCOUNT_ID": "paper-primary",
            "DEAD_MAN_ALERT_WEBHOOK_URL": ("https://alerts.example.test/dead-man"),
            "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID": ("dead-man-current"),
            "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": (
                base64.b64encode(b"d" * 32).decode("ascii")
            ),
        }
    )

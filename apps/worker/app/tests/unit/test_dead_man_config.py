from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.dead_man_config import DeadManSettings

CURRENT_ACK_KEY_B64 = base64.b64encode(b"d" * 32).decode("ascii")
PREVIOUS_ACK_KEY_B64 = base64.b64encode(b"r" * 32).decode("ascii")


def test_dead_man_settings_require_https_and_current_receiver_key() -> None:
    insecure_url = "http:" + "//alerts.example.test/events"
    with pytest.raises(ValidationError):
        DeadManSettings.model_validate(_settings(DEAD_MAN_ALERT_WEBHOOK_URL=insecure_url))
    values = _settings()
    values.pop("DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64")
    with pytest.raises(ValidationError):
        DeadManSettings.model_validate(values)


def test_dead_man_settings_accept_current_and_previous_receiver_keys() -> None:
    current_key_id = "dead-man-sensitive-current-id"
    previous_key_id = "dead-man-sensitive-previous-id"
    settings = DeadManSettings.model_validate(
        _settings(
            DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID=current_key_id,
            DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID=previous_key_id,
            DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64=(PREVIOUS_ACK_KEY_B64),
        )
    )

    assert settings.receiver_ack_key_ring().accepted_key_ids == (
        current_key_id,
        previous_key_id,
    )
    assert current_key_id not in repr(settings)
    assert previous_key_id not in repr(settings)
    assert "alert_webhook_receiver_ack_current_key_id" not in settings.model_dump()
    assert "alert_webhook_receiver_ack_previous_key_id" not in settings.model_dump()
    assert CURRENT_ACK_KEY_B64 not in repr(settings)
    assert PREVIOUS_ACK_KEY_B64 not in repr(settings)


def test_dead_man_settings_treat_exact_empty_previous_pair_as_absent() -> None:
    settings = DeadManSettings.model_validate(
        _settings(
            DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID="",
            DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64="",
        )
    )

    assert settings.receiver_ack_key_ring().accepted_key_ids == ("dead-man-current",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID": "previous"},
        {"DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64": (PREVIOUS_ACK_KEY_B64)},
        {"DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID": " "},
        {"DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64": " "},
    ],
)
def test_dead_man_settings_reject_incomplete_or_whitespace_previous_pair(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        DeadManSettings.model_validate(_settings(**overrides))


def test_dead_man_settings_do_not_consume_main_receiver_namespace() -> None:
    values = _settings()
    values.pop("DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID")
    values.pop("DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64")
    values["ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID"] = "main-current"
    values["ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64"] = CURRENT_ACK_KEY_B64

    with pytest.raises(ValidationError):
        DeadManSettings.model_validate(values)


@pytest.mark.parametrize(
    "name",
    [
        "ALERT_WEBHOOK_URL",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    ],
)
def test_dead_man_settings_reject_main_receiver_namespace_copresence(
    name: str,
) -> None:
    forbidden_marker = "main-namespace-input-marker"

    with pytest.raises(
        ValidationError,
        match="main_receiver_namespace_is_forbidden_on_dead_man",
    ) as error:
        DeadManSettings.model_validate(_settings(**{name: forbidden_marker}))

    assert forbidden_marker not in str(error.value)


def test_dead_man_validation_errors_hide_url_and_key_inputs() -> None:
    url_marker = "dead-man-url-input-marker"
    with pytest.raises(ValidationError) as url_error:
        DeadManSettings.model_validate(
            _settings(DEAD_MAN_ALERT_WEBHOOK_URL=(f"http://alerts.example.test/{url_marker}"))
        )
    assert url_marker not in str(url_error.value)

    key_marker = "dead-man-key-input-marker"
    with pytest.raises(ValidationError) as key_error:
        DeadManSettings.model_validate(
            _settings(DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=key_marker)
        )
    assert key_marker not in str(key_error.value)


def _settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": "not-a-" + "production-secret",
        "DEAD_MAN_ACCOUNT_ID": "paper-primary",
        "DEAD_MAN_ALERT_WEBHOOK_URL": ("https:" + "//alerts.example.test/dead-man"),
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID": "dead-man-current",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": CURRENT_ACK_KEY_B64,
    }
    return values | overrides

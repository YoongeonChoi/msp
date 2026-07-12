import pytest

from app.domain.trading.entities import BotSettings
from app.domain.trading.policies import settings_validation_reasons, validate_settings


def test_daily_order_limit_accepts_the_supported_boundaries() -> None:
    assert "invalid_max_daily_order_count" not in settings_validation_reasons(
        BotSettings(max_daily_order_count=1)
    )
    assert "invalid_max_daily_order_count" not in settings_validation_reasons(
        BotSettings(max_daily_order_count=1000)
    )


@pytest.mark.parametrize("value", [0, 1001])
def test_daily_order_limit_rejects_values_outside_the_storage_contract(value: int) -> None:
    settings = BotSettings(max_daily_order_count=value)

    assert "invalid_max_daily_order_count" in settings_validation_reasons(settings)
    with pytest.raises(ValueError, match="invalid_max_daily_order_count"):
        validate_settings(settings)


@pytest.mark.parametrize("value", [0, 100_000_001])
def test_order_amount_limit_rejects_values_outside_the_storage_contract(value: int) -> None:
    settings = BotSettings(max_order_amount_krw=value)

    assert "invalid_max_order_amount_krw" in settings_validation_reasons(settings)
    with pytest.raises(ValueError, match="invalid_max_order_amount_krw"):
        validate_settings(settings)


@pytest.mark.parametrize("value", [4, 3601])
def test_loop_interval_rejects_values_outside_the_storage_contract(value: int) -> None:
    settings = BotSettings(loop_interval_sec=value)

    assert "invalid_loop_interval_sec" in settings_validation_reasons(settings)


@pytest.mark.parametrize(
    "settings",
    [
        BotSettings(enabled=False, mode="live", live_order_allowed=True),
        BotSettings(enabled=True, mode="paper", live_order_allowed=True),
    ],
)
def test_live_permission_requires_enabled_live_state(settings: BotSettings) -> None:
    assert "live_order_allowed_state_invalid" in settings_validation_reasons(settings)

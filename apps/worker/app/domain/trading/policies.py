from __future__ import annotations

from .entities import BotSettings


def validate_settings(settings: BotSettings) -> None:
    if settings.mode not in {"paper", "live"}:
        raise ValueError("mode must be paper or live")
    if settings.max_order_amount_krw > 100_000_000:
        raise ValueError("max_order_amount_krw is unexpectedly high")
    if not 0 < settings.max_daily_loss_pct <= 0.20:
        raise ValueError("max_daily_loss_pct must be between 0 and 0.20")
    if not 0 < settings.max_position_pct <= 1:
        raise ValueError("max_position_pct must be between 0 and 1")
    if not 0 < settings.max_sector_pct <= 1:
        raise ValueError("max_sector_pct must be between 0 and 1")

